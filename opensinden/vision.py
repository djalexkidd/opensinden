"""Border detection and aim computation.

Reimplements the behaviour of the official driver:
  * a pixel matches when it is bright enough and close (RGB distance) to the
    border colour; the mask is reduced 2x2 ("any of the 4 pixels matches"),
  * the biggest convex quadrilateral blob is the screen border,
  * a perspective transform maps the camera aim point (image centre shifted by
    the calibration) into screen percentages (0..99, like the original).
"""

from dataclasses import dataclass, field

import cv2
import numpy as np

# The original maps the quad onto a 100x100 grid whose last index is 99.
GRID = 99.0


@dataclass
class VisionSettings:
    colour_rgb: tuple = (255, 255, 255)
    colour_range: int = 220
    min_brightness: tuple = (64, 64, 64)
    min_blob_size: int = 20
    close_mode: bool = True
    match_only_where_pointing: bool = False
    gangsta_setting: str = "2"          # "0"/"" off, "1" left handed, "2" right handed
    gunsight_offset: float = 4.9        # percent of the screen height
    camera_upside_down: bool = False


@dataclass
class DetectionResult:
    found: bool = False
    corners: list = field(default_factory=list)  # ordered quad in full-res pixels
    raw_corners: list = field(default_factory=list)  # TL, TR, BR, BL in camera space
    x: float = 0.0          # screen percentage before user offsets
    y: float = 0.0
    gangsta: int = 0
    full_match: bool = True
    aim: tuple = (0.0, 0.0)  # aim point in camera pixels
    new_calibration: tuple = None  # (x, y) percentages if computable
    mask: np.ndarray = None   # half resolution binary mask


_lut_cache = {}


def _luts(s: VisionSettings):
    key = (tuple(s.colour_rgb), tuple(s.min_brightness))
    if key not in _lut_cache:
        v = np.arange(256, dtype=np.float32)
        bgr_target = s.colour_rgb[::-1]
        bgr_min = s.min_brightness[::-1]
        dist = np.zeros((1, 256, 3), np.float32)
        bright = np.zeros((1, 256, 3), np.uint8)
        for c in range(3):
            dist[0, :, c] = (v - bgr_target[c]) ** 2
            bright[0, :, c] = np.where(v > bgr_min[c], 255, 0)
        _lut_cache.clear()
        _lut_cache[key] = (dist, bright)
    return _lut_cache[key]


def colour_mask(frame_bgr, s: VisionSettings):
    """Binary mask (half resolution) of pixels matching the border colour."""
    h, w = frame_bgr.shape[:2]
    h2, w2 = h // 2, w // 2
    img = frame_bgr[:h2 * 2, :w2 * 2]
    dist_lut, bright_lut = _luts(s)
    # Squared RGB distance to the border colour (LUT + channel sum, GIL-free).
    dist2 = cv2.transform(cv2.LUT(img, dist_lut), np.ones((1, 3), np.float32))
    close = cv2.compare(dist2, float(s.colour_range * s.colour_range), cv2.CMP_LE)
    # Bright enough: at least one channel above its minimum (saturating sum).
    bright = cv2.transform(cv2.LUT(img, bright_lut), np.ones((1, 3), np.float32))
    m = cv2.bitwise_and(close, bright)
    # 2x2 reduction: a cell matches if any of its 4 pixels matches.
    return cv2.resize(m, (w2, h2), interpolation=cv2.INTER_AREA).__gt__(0).astype(np.uint8) * 255


def order_corners(pts):
    """Return TL, TR, BR, BL (camera space) like the original (sort by X then Y)."""
    pts = sorted(pts, key=lambda p: p[0])
    left, right = pts[:2], pts[2:]
    tl, bl = (left[0], left[1]) if left[0][1] < left[1][1] else (left[1], left[0])
    tr, br = (right[0], right[1]) if right[0][1] < right[1][1] else (right[1], right[0])
    return [tl, tr, br, bl]


def orient_quad(tl, tr, br, bl, gangsta_setting):
    """Order the quad for the homography, handling the gun held on its side."""
    wide = np.hypot(tl[0] - tr[0], tl[1] - tr[1]) > np.hypot(tl[0] - bl[0], tl[1] - bl[1])
    if wide or gangsta_setting in ("", "0"):
        return [tl, tr, br, bl], 0
    if gangsta_setting != "1":
        return [tr, br, bl, tl], 2
    return [bl, tl, tr, br], 1


_DST = np.float32([[0, 0], [GRID, 0], [GRID, GRID], [0, GRID]])


def quad_to_percent(quad, x, y):
    m = cv2.getPerspectiveTransform(np.float32(quad), _DST)
    p = cv2.perspectiveTransform(np.float32([[[x, y]]]), m)[0, 0]
    return float(p[0]), float(p[1])


def percent_to_camera(quad, px, py):
    m = cv2.getPerspectiveTransform(_DST, np.float32(quad))
    p = cv2.perspectiveTransform(np.float32([[[px, py]]]), m)[0, 0]
    return float(p[0]), float(p[1])


def _quad_from_points(points):
    """Find the 4 corners of a convex blob outline, or None."""
    hull = cv2.convexHull(points.astype(np.int32))
    if len(hull) < 4:
        return None
    peri = cv2.arcLength(hull, True)
    for eps in (0.01, 0.02, 0.03, 0.04, 0.05, 0.07, 0.1):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) == 4:
            break
    else:
        return None
    if not cv2.isContourConvex(approx):
        return None
    quad = approx.reshape(4, 2).astype(np.float64)
    # Shape check: the hull must stay close to the quad edges (like AForge's
    # SimpleShapeChecker), otherwise this is not a rectangle seen in perspective.
    area_hull = cv2.contourArea(hull)
    area_quad = cv2.contourArea(approx)
    if area_quad <= 0 or abs(area_hull - area_quad) / area_quad > 0.08:
        return None
    return quad


class BorderDetector:
    def __init__(self, settings: VisionSettings):
        self.s = settings
        self.last_full_w = 0
        self.last_full_h = 0

    def aim_point(self, width, height, calibration):
        k = -1.0 if self.s.camera_upside_down else 1.0
        cx = width / 2.0 + calibration[0] / 100.0 * width * k
        cy = height / 2.0 + calibration[1] / 100.0 * height * k
        return cx, cy

    def _to_screen(self, quad_full, aim):
        tl, tr, br, bl = order_corners([tuple(p) for p in quad_full])
        oriented, gangsta = orient_quad(tl, tr, br, bl, self.s.gangsta_setting)
        x, y = quad_to_percent(oriented, aim[0], aim[1])
        return x, y, gangsta, oriented, [tl, tr, br, bl]

    def detect(self, frame_bgr, calibration=(0.0, 0.0)):
        s = self.s
        h, w = frame_bgr.shape[:2]
        mask = colour_mask(frame_bgr, s)
        mh, mw = mask.shape
        res = DetectionResult(mask=mask)
        aim = self.aim_point(w, h, calibration)
        res.aim = aim

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_area = 0.0
        for c in contours:
            bx, by, bw, bh = cv2.boundingRect(c)
            # AForge "coupled" size filtering: dropped only if both are too small.
            if bw < s.min_blob_size and bh < s.min_blob_size:
                continue
            pts = c.reshape(-1, 2)
            touches = []
            if s.close_mode:
                right = bx + bw >= mw
                bottom = by + bh >= mh
                left = bx <= 1
                top = by <= 1
                extra = []
                if right and bottom:
                    extra.append((mw - 1, mh - 1)); touches.append("br")
                if left and bottom:
                    extra.append((1, mh - 1)); touches.append("bl")
                if right and top:
                    extra.append((mw - 1, 1)); touches.append("tr")
                if left and top:
                    extra.append((1, 1)); touches.append("tl")
                if extra:
                    pts = np.vstack([pts, np.array(extra)])
            quad = _quad_from_points(pts)
            if quad is None:
                continue
            if s.match_only_where_pointing:
                quad_full = quad * 2.0 + 0.5
                x, y, _g, _o, _r = self._to_screen(quad_full, aim)
                if s.camera_upside_down:
                    x, y = 100.0 - x, 100.0 - y
                off = s.gunsight_offset
                if not (0 <= x <= 100 and 0 - off <= y <= 100 + off):
                    continue
            xs, ys = quad[:, 0], quad[:, 1]
            area = abs((xs.max() - xs.min()) * (ys.max() - ys.min()))
            if area > best_area:
                best_area = area
                best = (quad, (bx, by, bw, bh), touches)

        if best is None:
            return res

        quad, (bx, by, bw, bh), touches = best
        # Corners at full resolution (centre of the 2x2 cell).
        quad_full = quad * 2.0 + 0.5
        x, y, gangsta, oriented, raw = self._to_screen(quad_full, aim)
        res.found = True
        res.corners = [tuple(map(float, p)) for p in oriented]
        res.raw_corners = [tuple(map(float, p)) for p in raw]
        res.gangsta = gangsta
        res.full_match = not touches

        if touches:
            # Screen partially out of the camera view: rescale using the size of
            # the last complete match (same heuristic as the original).
            lw, lh = self.last_full_w, self.last_full_h
            if lw > 0 and lh > 0:
                for t in touches:
                    if t == "br":
                        x = x / 100.0 * bw / lw * 100.0
                        y = y / 100.0 * bh / lh * 100.0
                    elif t == "bl":
                        x = (x / 100.0 * bw + (lw - bw)) / lw * 100.0
                        y = y / 100.0 * bh / lh * 100.0
                    elif t == "tr":
                        x = x / 100.0 * bw / lw * 100.0
                        y = (y / 100.0 * bh + (lh - bh)) / lh * 100.0
                    elif t == "tl":
                        x = (x / 100.0 * bw + (lw - bw)) / lw * 100.0
                        y = (y / 100.0 * bh + (lh - bh)) / lh * 100.0
        else:
            self.last_full_w, self.last_full_h = bw, bh
            # Where the screen centre (with the sight offset) is in the camera:
            # used when the user shoots the centre in calibration mode.
            off = -s.gunsight_offset if s.camera_upside_down else s.gunsight_offset
            cx, cy = percent_to_camera(res.corners, 50.0, 50.0 + off)
            ncx = -1.0 * (w / 2.0 - cx) / w * 100.0
            ncy = -1.0 * (h / 2.0 - cy) / h * 100.0
            res.new_calibration = (ncx, ncy)

        if s.camera_upside_down:
            x = 50.0 - x + 50.0
            y = 50.0 - y + 50.0
        res.x, res.y = x, y
        return res


def calibration_from_detection(new_calibration, camera_upside_down):
    """Convert DetectionResult.new_calibration into the value stored in the gun."""
    if new_calibration is None:
        return None
    ncx, ncy = new_calibration
    if not (-20 < ncx < 20 and -20 < ncy < 20):
        return None
    k = -1.0 if camera_upside_down else 1.0
    return round(ncx, 2) * k, round(ncy, 2) * k


def apply_user_offsets(x, y, gangsta, x_ratio=1.0, x_offset=0.0, y_ratio=1.0,
                       y_offset=0.0, gunsight=4.9):
    """Same arithmetic as the original MoveMouse()."""
    x = x * x_ratio + x_offset
    y = y * y_ratio + y_offset
    if gangsta == 0:
        y -= gunsight
    elif gangsta == 2:
        x -= gunsight
    else:
        x += gunsight
    return max(0.0, min(100.0, x)), max(0.0, min(100.0, y))


def render_processed(mask, result: DetectionResult, show_rect=True):
    """Preview image of the processed mask (like the official 'Processed Image')."""
    vis = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
    vis = cv2.resize(vis, (mask.shape[1] * 2, mask.shape[0] * 2), interpolation=cv2.INTER_NEAREST)
    if result is not None and result.found and show_rect:
        pts = np.int32([[int(p[0]), int(p[1])] for p in result.corners])
        overlay = vis.copy()
        cv2.fillPoly(overlay, [pts], (255, 64, 32))
        # Hatching like the original preview.
        hatch = np.zeros_like(vis)
        for i in range(-vis.shape[0], vis.shape[1], 14):
            cv2.line(hatch, (i, vis.shape[0]), (i + vis.shape[0], 0), (255, 255, 255), 2)
        poly_mask = np.zeros(vis.shape[:2], np.uint8)
        cv2.fillPoly(poly_mask, [pts], 255)
        overlay[(poly_mask > 0) & (hatch[..., 0] > 0)] = (255, 255, 255)
        vis = cv2.addWeighted(overlay, 0.85, vis, 0.15, 0)
        cv2.polylines(vis, [pts], True, (0, 255, 0), 1)
    if result is not None:
        ax, ay = int(result.aim[0]), int(result.aim[1])
        cv2.drawMarker(vis, (ax, ay), (0, 0, 255), cv2.MARKER_CROSS, 16, 2)
    return vis
