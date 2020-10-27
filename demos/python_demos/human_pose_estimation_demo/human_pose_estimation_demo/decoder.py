import numpy as np
from openvino.inference_engine import IECore
from scipy.optimize import linear_sum_assignment


class AssociativeEmbeddingDecoder:

    def __init__(self, num_joints, max_num_people, detection_threshold, use_detection_val,
                 ignore_too_much, tag_threshold, tag_per_joint, nms_kernel,
                 adjust=True, refine=True, delta=0.0):

        self.num_joints = num_joints
        self.max_num_people = max_num_people
        self.detection_threshold = detection_threshold
        self.tag_threshold = tag_threshold
        self.tag_per_joint = tag_per_joint
        self.use_detection_val = use_detection_val
        self.ignore_too_much = ignore_too_much

        if self.num_joints == 17:
            self.joint_order = (0, 1, 2, 3, 4, 5, 6, 11, 12, 7, 8, 9, 10, 13, 14, 15, 16)
        else:
            self.joint_order = list(np.arange(self.num_joints))
        
        self.do_adjust = adjust
        self.do_refine = refine
        self.delta = delta

    def match(self, tag_k, loc_k, val_k):
        return list(map(self._match_by_tag, zip(tag_k, loc_k, val_k)))

    def _max_match(self, scores):
        r, c = linear_sum_assignment(scores)
        tmp = np.stack((r, c), axis=1)
        return tmp

    def _match_by_tag(self, inp):
        tag_k, loc_k, val_k = inp

        embd_size = tag_k.shape[2]

        class Pose:
            def __init__(self, num_joints, tag_size=1):
                self.num_joints = num_joints
                self.tag_size = tag_size
                self.pose = np.zeros((num_joints, 2 + 1 + tag_size), dtype=np.float32)
                self.pose_tag = np.zeros(tag_size, dtype=np.float32)
                self.valid_points_num = 0

            def add(self, idx, joint, tag):
                self.pose[idx] = joint
                self.pose_tag = (self.pose_tag * self.valid_points_num) + tag
                self.valid_points_num += 1
                self.pose_tag /= self.valid_points_num

            @property
            def tag(self):
                if self.valid_points_num > 0:
                    return self.pose_tag
                else:
                    return None

        all_joints = np.concatenate((loc_k, val_k[..., None], tag_k), -1)

        poses = []
        for idx in self.joint_order:
            tags = tag_k[idx]
            joints = all_joints[idx]
            mask = joints[:, 2] > self.detection_threshold
            tags = tags[mask]
            joints = joints[mask]

            if joints.shape[0] == 0:
                continue

            if len(poses) == 0:
                for tag, joint in zip(tags, joints):
                    pose = Pose(self.num_joints, embd_size)
                    pose.add(idx, joint, tag)
                    poses.append(pose)
            else:
                if self.ignore_too_much and len(poses) == self.max_num_people:
                    continue
                poses_tags = np.stack([p.tag for p in poses], axis=0)

                diff = tags[:, None] - poses_tags[None, :]
                diff_normed = np.linalg.norm(diff, ord=2, axis=2)
                diff_saved = np.copy(diff_normed)

                if self.use_detection_val:
                    diff_normed = np.round(diff_normed) * 100 - joints[:, 2:3]

                num_added = diff.shape[0]
                num_grouped = diff.shape[1]

                if num_added > num_grouped:
                    diff_normed = np.concatenate(
                        (diff_normed,
                        np.zeros((num_added, num_added - num_grouped),
                                dtype=np.float32) + 1e10),
                        axis=1)

                pairs = self._max_match(diff_normed)
                for row, col in pairs:
                    if row < num_added and col < num_grouped and diff_saved[row][col] < self.tag_threshold:
                        poses[col].add(idx, joints[row], tags[row])
                    else:
                        pose = Pose(self.num_joints, embd_size)
                        pose.add(idx, joints[row], tags[row])
                        poses.append(pose)

        if len(poses):
            ans = np.stack([p.pose for p in poses]).astype(np.float32)
            tags = np.stack([p.tag for p in poses]).astype(np.float32)
        else:
            ans = np.empty((0, self.num_joints, 2 + 1 + embd_size), dtype=np.float32)
            tags = np.empty((0, embd_size), dtype=np.float32)
        return ans, tags

    def top_k(self, heatmaps, tags):
        N, K, H, W = heatmaps.shape
        heatmaps = heatmaps.reshape(N, K, -1)
        ind = heatmaps.argpartition(-self.max_num_people, axis=2)[:, :, -self.max_num_people:]
        val_k = np.take_along_axis(heatmaps, ind, axis=2)

        tags = tags.reshape(N, K, W * H, -1)
        if not self.tag_per_joint:
            # FIXME.
            tags = tags.expand(-1, self.num_joints, -1, -1)
        tag_k = [np.take_along_axis(tags[..., i], ind, axis=2) for i in range(tags.shape[3])]
        tag_k = np.stack(tag_k, axis=3)

        x = ind % W
        y = ind // W
        ind_k = np.stack((x, y), axis=3)

        ans = {'tag_k': tag_k, 'loc_k': ind_k, 'val_k': val_k}
        return ans

    def adjust(self, ans, heatmaps):
        H, W = heatmaps.shape[-2:]
        for n, people in enumerate(ans):
            for person in people:
                for k, joint in enumerate(person):
                    heatmap = heatmaps[n, k]
                    px = int(joint[0])
                    py = int(joint[1])
                    if 1 < px < W - 1 and 1 < py < H - 1:
                        diff = np.array([
                            heatmap[py, px + 1] - heatmap[py, px - 1],
                            heatmap[py + 1, px] - heatmap[py - 1, px]
                        ])
                        joint[:2] += np.sign(diff) * .25
        return ans

    def get_peaks(self, tag, prev_tag, heatmap, mask):
        ids = np.where(mask)[0]
        if len(ids) == 0:
            return [], [], []
        tag = tag[mask]
        heatmap = heatmap[mask]

        diff = tag[..., 0].copy()
        diff -= prev_tag
        np.abs(diff, out=diff)
        np.floor(diff + 0.5, out=diff)
        K, H, W = heatmap.shape
        diff -= heatmap
        diff = diff.reshape(K, -1)
        idx = diff.argmin(axis=1)
        y, x = np.divmod(idx, W)
        return ids, y, x

    def refine(self, heatmap, tag, keypoints, pose_tag=None):
        K, H, W = heatmap.shape
        if len(tag.shape) == 3:
            tag = tag[..., None]

        if pose_tag is not None:
            prev_tag = pose_tag
        else:
            tags = []
            for i in range(K):
                if keypoints[i, 2] > 0:
                    # save tag value of detected keypoint
                    x, y = keypoints[i][:2].astype(int)
                    try:
                        tags.append(tag[i, y, x])
                    except IndexError as ex:
                        print(x, y, heatmap.shape, tag.shape)
                        raise ex 
            # mean tag of current detected person
            prev_tag = np.mean(tags, axis=0)

        # Allocate the buffer for tags similarity matrix.
        tag_copy = np.empty_like(tag[0, ..., 0])
        for i, (_heatmap, _tag) in enumerate(zip(heatmap, tag)):
            if keypoints[i, 2] > 0:
                continue
            tag_copy[...] = _tag[..., 0]
            diff = tag_copy
            diff -= prev_tag
            np.abs(diff, out=diff)
            np.floor(diff + 0.5, out=diff)
            diff -= _heatmap
            idx = diff.argmin()
            y, x = np.divmod(idx, _heatmap.shape[-1])

            # detection score at maximum position
            val = _heatmap[y, x]

            if val > 0:
                keypoints[i, :3] = x, y, val
                if 1 < x < W - 1 and 1 < y < H - 1:
                    diff = np.array([
                        _heatmap[y, x + 1] - _heatmap[y, x - 1],
                        _heatmap[y + 1, x] - _heatmap[y - 1, x]
                    ])
                    keypoints[i, :2] += np.sign(diff) * .25

        return keypoints

    def __call__(self, heatmaps, tags, nms_heatmaps=None):
        ans = self.match(**self.top_k(nms_heatmaps, tags))
        ans, ans_tags = map(list, zip(*ans))

        if self.do_adjust:
            ans = self.adjust(ans, heatmaps)

        if self.delta != 0.0:
            for people in ans:
                for person in people:
                    for joint in person:
                        joint[:2] += self.delta

        scores = [i[:, 2].mean() for i in ans[0]]

        if self.do_refine:
            ans = ans[0]
            heatmap_numpy = heatmaps[0]
            tag_numpy = tags[0]
            if not self.tag_per_joint:
                tag_numpy = np.tile(tag_numpy, (self.num_joints, 1, 1, 1))

            for i in range(len(ans)):
                ans[i] = self.refine(heatmap_numpy, tag_numpy, ans[i], ans_tags[0][i])
            ans = [ans]

        return ans, scores
