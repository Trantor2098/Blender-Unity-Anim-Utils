# SPDX-License-Identifier: GPL-2.0-or-later

from dataclasses import dataclass, field
import math
import re


_NATIVE_SECTIONS = {
    "m_RotationCurves": "rotation_curves",
    "m_EulerCurves": "euler_curves",
    "m_PositionCurves": "position_curves",
    "m_ScaleCurves": "scale_curves",
}
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")


@dataclass(frozen=True)
class UnityKeyframe:
    time: float
    value: float | tuple[float, ...]
    in_slope: float | tuple[float, ...] | None = None
    out_slope: float | tuple[float, ...] | None = None


@dataclass
class UnityCurve:
    path: str = ""
    attribute: str = ""
    keys: list[UnityKeyframe] = field(default_factory=list)


@dataclass
class UnityAnimationClip:
    name: str = "AnimationClip"
    sample_rate: float = 60.0
    start_time: float = 0.0
    stop_time: float | None = None
    rotation_curves: list[UnityCurve] = field(default_factory=list)
    euler_curves: list[UnityCurve] = field(default_factory=list)
    position_curves: list[UnityCurve] = field(default_factory=list)
    scale_curves: list[UnityCurve] = field(default_factory=list)
    float_curves: list[UnityCurve] = field(default_factory=list)
    editor_curves: list[UnityCurve] = field(default_factory=list)


def _parse_float(value):
    value = value.strip()
    if value in {"∞", "+∞", ".inf", "+.inf"}:
        return math.inf
    if value in {"-∞", "-.inf"}:
        return -math.inf
    return float(value)


def _parse_value(value):
    value = value.strip()
    if value.startswith("{"):
        components = []
        for item in value.strip("{} ").split(","):
            _, separator, component = item.partition(":")
            if separator:
                components.append(_parse_float(component))
        return tuple(components)
    return _parse_float(value)


def _finish_key(curve, key_data):
    if curve is None or not key_data or "time" not in key_data or "value" not in key_data:
        return
    curve.keys.append(UnityKeyframe(
        time=key_data["time"],
        value=key_data["value"],
        in_slope=key_data.get("inSlope"),
        out_slope=key_data.get("outSlope"),
    ))


def _combine_editor_components(component_curves, components):
    """Combine scalar EditorCurve components into one native-style vector curve."""
    if any(component not in component_curves for component in components):
        return None
    curves = [component_curves[component] for component in components]
    key_count = len(curves[0].keys)
    if not key_count or any(len(curve.keys) != key_count for curve in curves[1:]):
        return None
    combined_keys = []
    for keys in zip(*(curve.keys for curve in curves)):
        time = keys[0].time
        if any(abs(key.time - time) > 1.0e-6 for key in keys[1:]):
            return None
        combined_keys.append(UnityKeyframe(
            time=time,
            value=tuple(key.value for key in keys),
            in_slope=tuple(key.in_slope for key in keys),
            out_slope=tuple(key.out_slope for key in keys),
        ))
    return UnityCurve(path=curves[0].path, keys=combined_keys)


def _apply_editor_curves(clip):
    """Prefer full-path m_EditorCurves when native paths were truncated."""
    groups = {}
    for curve in clip.editor_curves:
        prefix, separator, component = curve.attribute.rpartition('.')
        if separator:
            groups.setdefault((curve.path, prefix), {})[component] = curve

    replacements = {
        'm_LocalRotation': ('rotation_curves', 'xyzw'),
        'm_LocalPosition': ('position_curves', 'xyz'),
        'm_LocalScale': ('scale_curves', 'xyz'),
        'localEulerAngles': ('euler_curves', 'xyz'),
    }
    combined_by_kind = {kind: [] for kind, _components in replacements.values()}
    quaternion_paths = set()
    for (path, prefix), component_curves in groups.items():
        replacement = replacements.get(prefix)
        if replacement is None:
            continue
        kind, components = replacement
        combined = _combine_editor_components(component_curves, components)
        if combined is None:
            continue
        if kind == 'rotation_curves':
            quaternion_paths.add(path)
        combined_by_kind[kind].append(combined)

    # Quaternion editor curves are authoritative when both quaternion and Euler
    # representations exist for the same Transform.
    combined_by_kind['euler_curves'] = [
        curve for curve in combined_by_kind['euler_curves']
        if curve.path not in quaternion_paths
    ]
    for kind, curves in combined_by_kind.items():
        if curves:
            merged = {curve.path: curve for curve in getattr(clip, kind)}
            merged.update((curve.path, curve) for curve in curves)
            setattr(clip, kind, list(merged.values()))


def load(filepath):
    with open(filepath, "r", encoding="utf-8-sig") as handle:
        return parse(handle.read())


def parse(text):
    clip = UnityAnimationClip()
    section = None
    current_curve = None
    current_key = None
    path_continuation = False

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        # Unity YAML wraps long unquoted path scalars onto more-indented lines.
        # Fold them back with a space exactly as YAML plain-scalar semantics do.
        if path_continuation:
            if indent > 4:
                current_curve.path += " " + stripped
                continue
            path_continuation = False

        if indent == 2 and stripped.startswith("m_Name:"):
            clip.name = stripped.partition(":")[2].strip() or clip.name
            continue
        if indent == 2 and stripped.startswith("m_SampleRate:"):
            clip.sample_rate = _parse_float(stripped.partition(":")[2])
            continue
        if indent == 4 and stripped.startswith("m_StartTime:"):
            clip.start_time = _parse_float(stripped.partition(":")[2])
            continue
        if indent == 4 and stripped.startswith("m_StopTime:"):
            clip.stop_time = _parse_float(stripped.partition(":")[2])
            continue

        if indent == 2 and stripped.startswith("m_") and ":" in stripped:
            _finish_key(current_curve, current_key)
            current_key = None
            section_name = stripped.partition(":")[0]
            section = _NATIVE_SECTIONS.get(section_name)
            if section_name == "m_FloatCurves":
                section = "float_curves"
            elif section_name == "m_EditorCurves":
                section = "editor_curves"
            current_curve = None
            continue

        if section is None:
            continue
        if section == "editor_curves" and indent == 2 and stripped.startswith("- serializedVersion:"):
            _finish_key(current_curve, current_key)
            current_key = None
            current_curve = UnityCurve()
            clip.editor_curves.append(current_curve)
            continue
        if indent == 2 and stripped == "- curve:":
            _finish_key(current_curve, current_key)
            current_key = None
            current_curve = UnityCurve()
            getattr(clip, section).append(current_curve)
            continue
        if current_curve is None:
            continue
        if indent == 6 and stripped.startswith("- serializedVersion:"):
            _finish_key(current_curve, current_key)
            current_key = {}
            continue
        if current_key is not None and indent == 8:
            key, separator, value = stripped.partition(":")
            if separator and key in {"time", "value", "inSlope", "outSlope"}:
                current_key[key] = _parse_value(value)
                continue
        if indent == 4 and stripped.startswith("path:"):
            _finish_key(current_curve, current_key)
            current_key = None
            current_curve.path = stripped.partition(":")[2].strip()
            path_continuation = True
            continue
        if indent == 4 and stripped.startswith("attribute:"):
            _finish_key(current_curve, current_key)
            current_key = None
            current_curve.attribute = stripped.partition(":")[2].strip()

    _finish_key(current_curve, current_key)
    _apply_editor_curves(clip)
    return clip
