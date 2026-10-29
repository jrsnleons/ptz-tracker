"""YOLOv8 head detector (same ONNX model as the Windows tracker)."""
import os

import cv2
import numpy as np
import onnxruntime as ort


def _session(path):
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    # CoreML uses the Apple GPU / Neural Engine; fall back to CPU if it fails to load.
    for providers in (["CoreMLExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]):
        try:
            return ort.InferenceSession(path, sess_options=opts, providers=providers)
        except Exception:
            continue
    raise RuntimeError(f"could not load model {path}")


class HeadDetector:
    def __init__(self, model_path, conf=0.4, nms=0.45):
        self.session = _session(model_path)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.in_h, self.in_w = int(inp.shape[2]), int(inp.shape[3])  # 352 x 640
        self.conf = conf
        self.nms = nms
        self.provider = self.session.get_providers()[0]

    def detect(self, frame_bgr):
        """Returns an (N, 5) array of [x1, y1, x2, y2, score] in frame pixels."""
        h, w = frame_bgr.shape[:2]
        # letterbox into the 640x352 input so heads keep their shape for any aspect ratio
        scale = min(self.in_w / w, self.in_h / h)
        nw, nh = round(w * scale), round(h * scale)
        px, py = (self.in_w - nw) // 2, (self.in_h - nh) // 2
        img = np.full((self.in_h, self.in_w, 3), 114, dtype=np.uint8)
        img[py : py + nh, px : px + nw] = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        tensor = img.transpose(2, 0, 1)[np.newaxis]
        out = self.session.run(None, {self.input_name: tensor})[0][0]  # (5, N)
        out = out.T
        scores = out[:, 4]
        keep = scores > self.conf
        out, scores = out[keep], scores[keep]
        if len(out) == 0:
            return np.zeros((0, 5), dtype=np.float32)

        cx, cy = (out[:, 0] - px) / scale, (out[:, 1] - py) / scale
        bw, bh = out[:, 2] / scale, out[:, 3] / scale
        boxes_xywh = np.stack([cx - bw / 2, cy - bh / 2, bw, bh], axis=1)
        idx = cv2.dnn.NMSBoxes(boxes_xywh.tolist(), scores.tolist(), self.conf, self.nms)
        idx = np.array(idx).reshape(-1)
        b = boxes_xywh[idx]
        return np.stack([b[:, 0], b[:, 1], b[:, 0] + b[:, 2], b[:, 1] + b[:, 3], scores[idx]], axis=1)


def default_model_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "models", "head_640_v8s.onnx")
