# SPDX-License-Identifier: GPL-2.0-or-later

import math
import zlib

import bpy
from bpy_extras import anim_utils
from mathutils import Euler, Matrix, Quaternion, Vector

from .humanoid_biped_profile import (
    BIPED_HUMANOID_PROFILE,
    DEFAULT_HUMANOID_PRESET,
    HUMANOID_PRESETS,
)


def _finite_slope(slope, fallback):
    if slope is None or not math.isfinite(slope):
        return fallback
    return slope


def _evaluate_component(keys, time, component):
    if not keys:
        return None
    if time <= keys[0].time:
        value = keys[0].value
        return value[component] if isinstance(value, tuple) else value
    if time >= keys[-1].time:
        value = keys[-1].value
        return value[component] if isinstance(value, tuple) else value

    for left, right in zip(keys, keys[1:]):
        if time > right.time:
            continue
        duration = right.time - left.time
        if duration <= 0.0:
            value = right.value
            return value[component] if isinstance(value, tuple) else value
        left_value = left.value[component] if isinstance(left.value, tuple) else left.value
        right_value = right.value[component] if isinstance(right.value, tuple) else right.value
        secant = (right_value - left_value) / duration
        left_slope = left.out_slope
        right_slope = right.in_slope
        if isinstance(left_slope, tuple):
            left_slope = left_slope[component]
        if isinstance(right_slope, tuple):
            right_slope = right_slope[component]
        left_slope = _finite_slope(left_slope, secant)
        right_slope = _finite_slope(right_slope, secant)
        factor = (time - left.time) / duration
        factor2 = factor * factor
        factor3 = factor2 * factor
        return (
            (2.0 * factor3 - 3.0 * factor2 + 1.0) * left_value
            + (factor3 - 2.0 * factor2 + factor) * duration * left_slope
            + (-2.0 * factor3 + 3.0 * factor2) * right_value
            + (factor3 - factor2) * duration * right_slope
        )
    return None


def _evaluate_curve(curve, time, size, default):
    if curve is None or not curve.keys:
        return tuple(default)
    return tuple(_evaluate_component(curve.keys, time, component) for component in range(size))


def _unity_to_fbx_vector(value):
    """Convert a Unity local translation to the FBX skeleton space kept by the importer.

    The imported armature object (or its baked armature data) already owns the Y-up to
    Z-up conversion. Applying that conversion to every local bone transform a second
    time twists the hierarchy. Unity and the FBX skeleton differ only in handedness
    here, represented by an X reflection.
    """
    return Vector((-value[0], value[1], value[2]))


def _unity_to_fbx_rotation(unity_quat):
    # Reflection conjugation B R B, B=diag(-1, 1, 1). For an axial vector
    # such as a quaternion's imaginary part this maps (x, y, z) to (x, -y, -z).
    return Quaternion((unity_quat.w, unity_quat.x, -unity_quat.y, -unity_quat.z))


def _unity_to_fbx_quaternion(value):
    return _unity_to_fbx_rotation(Quaternion((value[3], value[0], value[1], value[2])))


def _matrix_from_property(value):
    if not value or len(value) != 16:
        return None
    return Matrix(tuple(tuple(value[row * 4 + column] for column in range(4)) for row in range(4)))


def _bone_retarget_corrections(armature):
    """Return C for target_local = C_parent^-1 @ source_local @ C.

    The FBX importer stores the un-oriented source rest matrix on each bone. Comparing
    it with the final Blender rest matrix captures Better-FBX bone orientation as well
    as an optional baked root-axis conversion.
    """
    corrections = {}
    baked_axis = _matrix_from_property(armature.get("unity_fbx_baked_axis_matrix")) or Matrix()
    for bone in armature.data.bones:
        source_rest = _matrix_from_property(bone.get("unity_fbx_source_rest"))
        if source_rest is None:
            source_rest = bone.matrix_local.copy()
            if bone.parent:
                source_rest = bone.parent.matrix_local.inverted_safe() @ source_rest

        target_rest = bone.matrix_local.copy()
        parent_correction = Matrix()
        if bone.parent:
            target_rest = bone.parent.matrix_local.inverted_safe() @ target_rest
            parent_correction = corrections[bone.parent.name]
        else:
            # Baking the armature object's axis conversion left-multiplies only its
            # root bones. It is a coordinate-space change, not a bone-axis correction.
            source_rest = baked_axis @ source_rest
        corrections[bone.name] = source_rest.inverted_safe() @ parent_correction @ target_rest
    return corrections


def _bone_paths(armature):
    full_paths = {}
    suffix_paths = {}
    for bone in armature.data.bones:
        names = []
        current = bone
        while current:
            names.append(current.name)
            current = current.parent
        path = "/".join(reversed(names))
        full_paths[path] = bone.name
        full_paths[f"path_{zlib.crc32(path.encode('utf-8')) & 0xffffffff}"] = bone.name
        source_path = bone.get("unity_fbx_path")
        if source_path:
            full_paths[source_path] = bone.name
            # Unity stores some native Transform bindings as the unsigned CRC32
            # of their hierarchy path instead of preserving the path string.
            path_hash = zlib.crc32(source_path.encode("utf-8")) & 0xffffffff
            full_paths[f"path_{path_hash}"] = bone.name
        parts = path.split("/")
        for index in range(len(parts)):
            suffix_paths.setdefault("/".join(parts[index:]), []).append(bone.name)
    return full_paths, suffix_paths


def _resolve_bone(path, full_paths, suffix_paths, armature):
    if path in full_paths:
        return full_paths[path]
    matches = suffix_paths.get(path, ())
    if len(matches) == 1:
        return matches[0]
    # A clip can include a model root which is absent from the imported armature.
    # Try successively shorter path suffixes before falling back to the leaf name.
    parts = path.split('/')
    for index in range(1, len(parts) - 1):
        suffix = "/".join(parts[index:])
        if suffix in full_paths:
            return full_paths[suffix]
        matches = suffix_paths.get(suffix, ())
        if len(matches) == 1:
            return matches[0]
    # A leaf-only binding can safely fall back to a unique Blender bone name.
    # Never do this for a failed hierarchy path: clips may contain unrelated
    # helper/end nodes also named "Bip001", which would otherwise bind to the
    # real armature root and create duplicate or destructive root curves.
    if '/' in path:
        return None
    leaf = path.rsplit("/", 1)[-1]
    return leaf if leaf in armature.data.bones and sum(b.name == leaf for b in armature.data.bones) == 1 else None


def _float_curve_groups(clip):
    groups = {}
    for curve in clip.float_curves:
        prefix, separator, component = curve.attribute.partition('.')
        if separator and prefix in {'RootT', 'RootQ', 'MotionT', 'MotionQ'}:
            groups.setdefault(prefix, {})[component] = curve
    return groups


def _evaluate_float_group(group, time, components, default):
    return tuple(
        _evaluate_component(group[component].keys, time, 0) if component in group else fallback
        for component, fallback in zip(components, default)
    )


def _scaled_quaternion_delta(delta, factor):
    """Scale a calibrated endpoint rotation while preserving its axis."""
    delta = delta.normalized()
    if delta.w < 0.0:
        delta.negate()
    axis, angle = delta.to_axis_angle()
    return Quaternion(axis, angle * factor)


def _profile_quaternion(value):
    """Read a calibrated quaternion already converted to mathutils WXYZ order."""
    return Quaternion(value).normalized()


def _profile_delta(base, endpoint, side):
    return endpoint @ base.inverted() if side == "left" else base.inverted() @ endpoint


def _build_muscle_degree_ranges():
    """Derive each muscle's signed degree range from its primary profile bone."""
    ranges = {}
    for profile in BIPED_HUMANOID_PROFILE.values():
        base = _profile_quaternion(profile["base"])
        side = profile.get("side", "right")
        for attribute, minus_value, plus_value in profile["effects"]:
            if attribute in ranges:
                continue
            minus = _profile_delta(base, _profile_quaternion(minus_value), side).angle
            plus = _profile_delta(base, _profile_quaternion(plus_value), side).angle
            ranges[attribute] = (math.degrees(minus), math.degrees(plus))
    return ranges


_MUSCLE_DEGREE_RANGES = _build_muscle_degree_ranges()


# HumanPose.bodyPosition/bodyRotation describe the Avatar's body center, not the
# Transform mapped as Hips.  These values were sampled from Unity's
# HumanPoseHandler for the Bip001-root Avatar preset.  Unity's solver moves that
# center as limbs and the torso rotate, so both a neutral offset and the muscle
# contributions are required; treating RootT/RootQ as the Hips transform leaves
# a large, pose-dependent root error.
_BIP001_ROOT_HUMAN_SCALE = 1.05778217
_BIP001_ROOT_HEIGHT_OFFSET = 0.1
_BIP001_ROOT_BASE_ROTATION = (
    0.4868747, -0.51278913, 0.512783945, 0.4868808,
)
_BIP001_ROOT_BASE_POSITION = (
    -0.07830812, -0.0892602, -0.04978663,
)
_BIP001_ROOT_ROTATION_EFFECTS = (
    ('Spine Front-Back', (0.336276531, -0.6220299, 0.62202245, 0.33628124), (0.606375933, -0.3637366, 0.363732547, 0.606384933)),
    ('Spine Left-Right', (0.6028969, -0.365091532, 0.625888, 0.3338979), (0.333890826, -0.6258909, 0.365084678, 0.602902)),
    ('Spine Twist Left-Right', (0.369783759, -0.405689657, 0.599070847, 0.5829152), (0.582906961, -0.5990784, 0.405684978, 0.3697895)),
    ('Chest Front-Back', (0.366635978, -0.6046325, 0.6046256, 0.366641045), (0.5859179, -0.395850033, 0.395846128, 0.585925639)),
    ('Chest Left-Right', (0.5847946, -0.396987557, 0.6062791, 0.364474922), (0.364468247, -0.606282353, 0.39698118, 0.5847997)),
    ('Chest Twist Left-Right', (0.369751424, -0.405720532, 0.599047363, 0.5829384), (0.58293134, -0.5990536, 0.405716777, 0.36975646)),
    ('UpperChest Front-Back', (0.4529714, -0.5429708, 0.5429652, 0.45297727), (0.5185091, -0.48077786, 0.4807729, 0.5185157)),
    ('UpperChest Left-Right', (0.518676, -0.4805788, 0.54303056, 0.4529207), (0.452914149, -0.5430353, 0.4805731, 0.518681943)),
    ('UpperChest Twist Left-Right', (0.4301472, -0.462537974, 0.557951331, 0.5382586), (0.538251936, -0.5579572, 0.462533385, 0.430152953)),
    ('Left Shoulder Down-Up', (0.485840082, -0.513876438, 0.5116944, 0.487913281), (0.483148754, -0.5166835, 0.508859754, 0.490578383)),
    ('Left Shoulder Front-Back', (0.493776143, -0.5360976, 0.5041074, 0.463314354), (0.477852225, -0.490461677, 0.5192267, 0.5113788)),
    ('Right Shoulder Down-Up', (0.487906963, -0.5116997, 0.513871133, 0.48584646), (0.490572572, -0.5088646, 0.5166788, 0.4831546)),
    ('Right Shoulder Front-Back', (0.463308454, -0.5041124, 0.5360922, 0.493782371), (0.5113724, -0.5192319, 0.490456849, 0.477858275)),
)
_BIP001_ROOT_POSITION_EFFECTS = (
    ('Spine Front-Back', (-0.07830794, -0.111812316, -0.07253298), (-0.07830849, -0.06885903, -0.0158145726)),
    ('Spine Left-Right', (-0.03868101, -0.05209431, -0.0513704866), (-0.09629877, -0.1302002, -0.04798915)),
    ('Spine Twist Left-Right', (-0.0831567943, -0.08993271, -0.0159281753), (-0.06060084, -0.088471055, -0.0764505)),
    ('Chest Front-Back', (-0.07830799, -0.101503022, -0.0638149), (-0.0783082247, -0.06953217, -0.02789485)),
    ('Chest Left-Right', (-0.052620545, -0.05417593, -0.0514129363), (-0.0896901339, -0.117952473, -0.0480148867)),
    ('Chest Twist Left-Right', (-0.08316132, -0.08994267, -0.0159273818), (-0.0605954, -0.08846461, -0.07644998)),
    ('UpperChest Front-Back', (-0.0783081055, -0.0910724, -0.0521530136), (-0.0783081651, -0.08470379, -0.0462157056)),
    ('UpperChest Left-Right', (-0.0744805858, -0.0783703253, -0.0502864979), (-0.08080347, -0.0974391252, -0.04927917)),
    ('UpperChest Twist Left-Right', (-0.08249578, -0.08986259, -0.0334812552), (-0.07089215, -0.08868671, -0.06427945)),
    ('Neck Nod Down-Up', (-0.0783081353, -0.0884512439, -0.05242725), (-0.07830813, -0.0881824344, -0.0472438633)),
    ('Neck Tilt Left-Right', (-0.07571296, -0.0883167461, -0.0498355664), (-0.0809033141, -0.0883168057, -0.0498355553)),
    ('Left Upper Leg Front-Back', (-0.07830737, -0.159396023, -0.087745), (-0.07830797, -0.09624171, -0.00263708713)),
    ('Left Upper Leg In-Out', (-0.12515752, -0.11195223, -0.03506478), (-0.0314581878, -0.111951157, -0.0350649767)),
    ('Left Upper Leg Twist In-Out', (-0.0645216554, -0.09359211, -0.0564640425), (-0.0920944661, -0.09359254, -0.05646399)),
    ('Left Lower Leg Stretch', (-0.0783078745, -0.11001347, -0.04870564), (-0.07830818, -0.08672091, -0.07041227)),
    ('Right Upper Leg Front-Back', (-0.0783074, -0.159396023, -0.08774508), (-0.07830797, -0.09624171, -0.002637076)),
    ('Right Upper Leg In-Out', (-0.03145815, -0.111951038, -0.0350649543), (-0.12515749, -0.11195223, -0.03506478)),
    ('Right Upper Leg Twist In-Out', (-0.09209444, -0.09359254, -0.0564639829), (-0.0645216554, -0.09359211, -0.0564640462)),
    ('Right Lower Leg Stretch', (-0.0783078745, -0.11001347, -0.04870565), (-0.07830818, -0.08672091, -0.07041228)),
    ('Left Shoulder Down-Up', (-0.07914823, -0.0875788, -0.0497866236), (-0.0761804357, -0.09504924, -0.04978664)),
    ('Left Shoulder Front-Back', (-0.07650178, -0.09116037, -0.05379025), (-0.07971948, -0.08805149, -0.0453347638)),
    ('Left Arm Down-Up', (-0.08409367, -0.08672779, -0.0473288633), (-0.07953317, -0.09927261, -0.0473258719)),
    ('Left Arm Front-Back', (-0.08693193, -0.0964077339, -0.04755345), (-0.07694969, -0.09054753, -0.0385198332)),
    ('Left Arm Twist In-Out', (-0.0788069144, -0.08633267, -0.0465508439), (-0.0750491545, -0.09117598, -0.0475509539)),
    ('Left Forearm Stretch', (-0.07972809, -0.09105265, -0.0464384966), (-0.07520611, -0.0871799663, -0.0482099839)),
    ('Right Shoulder Down-Up', (-0.07709358, -0.0866033062, -0.04978662), (-0.08087457, -0.0935082957, -0.0497866161)),
    ('Right Shoulder Front-Back', (-0.07954368, -0.09052772, -0.0443727151), (-0.07680733, -0.0874189, -0.0547192954)),
    ('Right Arm Down-Up', (-0.07074966, -0.08595301, -0.046575442), (-0.0767095461, -0.10234043, -0.04657294)),
    ('Right Arm Front-Back', (-0.06716407, -0.09821174, -0.0482698344), (-0.07896655, -0.09168922, -0.035634473)),
    ('Right Arm Twist In-Out', (-0.07780989, -0.0863330141, -0.0465505049), (-0.0815671, -0.09117653, -0.0475514531)),
    ('Right Forearm Stretch', (-0.07688838, -0.09105286, -0.0464385375), (-0.08141045, -0.08718028, -0.04821007)),
)


def _normalized_muscle_factor(attribute, value, values_are_degrees):
    factor = abs(value)
    if values_are_degrees:
        degree_range = _MUSCLE_DEGREE_RANGES.get(attribute)
        if degree_range:
            limit = degree_range[1 if value >= 0.0 else 0]
            if limit > 1.0e-8:
                factor /= limit
    return factor


def _evaluate_humanoid_rotation(
        profile, muscle_curves, time, _source_rest_rotation, values_are_degrees):
    """Evaluate the calibrated Avatar's absolute Unity local rotation."""
    base = _profile_quaternion(profile["base"])
    rotation = base.copy()
    side = profile.get("side", "right")
    base_inverse = base.inverted()

    for attribute, minus_value, plus_value in profile["effects"]:
        curve_data = muscle_curves.get(attribute)
        if curve_data is None:
            continue
        value = _evaluate_filtered_muscle(curve_data, time)
        if value is None or abs(value) < 1.0e-12:
            continue
        endpoint = _profile_quaternion(plus_value if value >= 0.0 else minus_value)
        factor = _normalized_muscle_factor(attribute, value, values_are_degrees)
        if side == "left":
            delta = endpoint @ base_inverse
            rotation = _scaled_quaternion_delta(delta, factor) @ rotation
        else:
            delta = base_inverse @ endpoint
            rotation = rotation @ _scaled_quaternion_delta(delta, factor)
    # The profile was sampled from the supported Bip001 Avatar itself, so these
    # are already absolute Transform-local rotations, not generic deltas around
    # the selected FBX bone's rest pose. Applying another rest correction here
    # changes their axes (most visibly on thighs and arms). The regular source to
    # Blender-bone correction later in import_clip still handles display axes.
    return _unity_to_fbx_rotation(rotation).normalized()


def _muscle_attribute_alias(attribute):
    """Translate serialized finger names to Unity's HumanTrait muscle names."""
    for side in ("Left", "Right"):
        prefix = side + "Hand."
        if attribute.startswith(prefix):
            return side + " " + attribute[len(prefix):].replace('.', ' ')
    return attribute


def _muscle_values_are_degrees(curves):
    """Detect exporters that serialize Humanoid muscles in degrees.

    Unity's native muscle values are normalized, but a few text exporters emit
    degrees instead.  Do not decide from a single value outside [-2, 2]: dense
    clips in the wild contain isolated corrupt samples and legitimate overshoot.
    A degree-valued clip instead has large values sustained by several complete
    muscle curves.
    """
    sustained_degree_curves = 0
    recognized_curves = 0
    for curve in curves:
        if _muscle_attribute_alias(curve.attribute) not in _MUSCLE_DEGREE_RANGES:
            continue
        values = sorted(
            abs(float(key.value)) for key in curve.keys
            if math.isfinite(float(key.value))
        )
        if not values:
            continue
        recognized_curves += 1
        # The median makes the classification insensitive to one-frame spikes.
        if values[len(values) // 2] > 5.0:
            sustained_degree_curves += 1

    # Requiring more than one curve prevents an isolated damaged channel from
    # changing the interpretation (and therefore the amplitude) of every bone.
    return sustained_degree_curves >= 2 and sustained_degree_curves * 20 >= recognized_curves


def _has_misaligned_humanoid_data(curves, float_groups):
    """Detect the broken scalar table produced by some AnimationClip extractors."""
    recognized = [
        curve for curve in curves
        if _muscle_attribute_alias(curve.attribute) in _MUSCLE_DEGREE_RANGES
    ]
    if len(recognized) < 20:
        return False
    constant_count = sum(_curve_is_constant(curve) for curve in recognized)
    if constant_count * 5 < len(recognized) * 4:
        return False

    root_rotation = float_groups.get('RootQ', {})
    if len(root_rotation) != 4:
        return False
    return all(
        abs(float(key.value)) < 1.0e-8
        for curve in root_rotation.values()
        for key in curve.keys
    )


def _filtered_muscle_values(curve, values_are_degrees=False):
    """Remove isolated corrupt samples from dense serialized Humanoid curves.

    Some exported text clips contain one-frame values tens of times outside their
    neighboring muscle samples. A local median identifies those spikes without
    smoothing every valid key in fast actions.
    """
    values = [float(key.value) for key in curve.keys]
    if len(values) < 3:
        return values

    # Replace only conspicuous local outliers.  A blanket median/average filter
    # changes valid fast attacks and can alter a joint by tens of degrees.
    source = values
    filtered = list(values)
    minimum_deviation = 15.0 if values_are_degrees else 0.5
    for index, value in enumerate(source):
        start = max(0, index - 2)
        stop = min(len(filtered), index + 3)
        window = sorted(source[start:stop])
        median = window[len(window) // 2]
        deviations = sorted(abs(sample - median) for sample in window)
        mad = deviations[len(deviations) // 2]
        if (not math.isfinite(value)
                or abs(value - median) > max(minimum_deviation, 6.0 * mad)):
            filtered[index] = median
    return filtered


def _evaluate_filtered_muscle(curve_data, time):
    curve, values = curve_data
    keys = curve.keys
    if not keys:
        return None
    if time <= keys[0].time:
        return values[0]
    if time >= keys[-1].time:
        return values[-1]
    for index, (left, right) in enumerate(zip(keys, keys[1:])):
        if time > right.time:
            continue
        duration = right.time - left.time
        if duration <= 0.0:
            return values[index + 1]
        factor = (time - left.time) / duration
        factor2 = factor * factor
        # These dense Humanoid curves use zero tangents. Smoothstep matches their
        # Hermite interpolation without reusing a corrupt sample's slope.
        factor = factor2 * (3.0 - 2.0 * factor)
        return values[index] + (values[index + 1] - values[index]) * factor
    return values[-1]


def _curve_is_constant(curve, tolerance=1.0e-6):
    if curve is None or len(curve.keys) < 2:
        return True
    initial = curve.keys[0].value
    if isinstance(initial, tuple):
        return all(
            max(abs(float(value) - float(reference))
                for value, reference in zip(key.value, initial)) <= tolerance
            for key in curve.keys[1:]
        )
    return all(abs(float(key.value) - float(initial)) <= tolerance
               for key in curve.keys[1:])


def _without_constant_upper_arm_twist_channels(bone_name, curves):
    """Drop static Unity-rest channels that should leave the FBX bind pose intact.

    The GI clips serialize constant TRS values for these helper bones in Unity's
    imported-FBX coordinate frame.  Applying those values as raw FBX-local
    transforms creates a false offset; their actual Unity sampled pose is the
    model's unchanged bind transform.
    """
    if not bone_name.startswith('+UpperArmTwist '):
        return curves
    return {
        kind: curve for kind, curve in curves.items()
        if kind not in {'rotation', 'euler', 'position'} or not _curve_is_constant(curve)
    }


def _bip001_root_avatar_transform(muscle_curves, time, values_are_degrees):
    """Approximate Unity's body-center-to-Hips solve for the root-Hips preset."""
    base_rotation = _profile_quaternion(_BIP001_ROOT_BASE_ROTATION)
    rotation = base_rotation.copy()
    base_inverse = base_rotation.inverted()
    for attribute, minus_value, plus_value in _BIP001_ROOT_ROTATION_EFFECTS:
        curve_data = muscle_curves.get(attribute)
        if curve_data is None:
            continue
        value = _evaluate_filtered_muscle(curve_data, time)
        if value is None or abs(value) < 1.0e-12:
            continue
        endpoint = _profile_quaternion(plus_value if value >= 0.0 else minus_value)
        factor = _normalized_muscle_factor(attribute, value, values_are_degrees)
        # These are effects on the body-center-to-Hips transform and therefore
        # compose on the left, unlike most Transform-local bone rotations.
        delta = endpoint @ base_inverse
        rotation = _scaled_quaternion_delta(delta, factor) @ rotation

    base_position = Vector(_BIP001_ROOT_BASE_POSITION)
    position = base_position.copy()
    for attribute, minus_value, plus_value in _BIP001_ROOT_POSITION_EFFECTS:
        curve_data = muscle_curves.get(attribute)
        if curve_data is None:
            continue
        value = _evaluate_filtered_muscle(curve_data, time)
        if value is None or abs(value) < 1.0e-12:
            continue
        endpoint = Vector(plus_value if value >= 0.0 else minus_value)
        factor = _normalized_muscle_factor(attribute, value, values_are_degrees)
        position += (endpoint - base_position) * factor
    return position, rotation.normalized()


def _root_motion_source_matrices(
        group, times, source_rest, muscle_curves=None,
        values_are_degrees=False, calibrate_bip001_root=False):
    source_loc, source_rot, _source_scale = source_rest.decompose()
    unity_loc_default = (-source_loc.x, source_loc.y, source_loc.z)
    # RootQ/MotionQ are body-root rotations relative to the Avatar's neutral
    # Hips transform, unlike native Transform curves which are absolute local
    # rotations.  The neutral rotation is the FBX source rest below.
    unity_rot_default = (0.0, 0.0, 0.0, 1.0)
    matrices = []
    for time in times:
        location = _unity_to_fbx_vector(_evaluate_float_group(
            group.get('translation', {}), time, 'xyz', unity_loc_default))
        rotation_value = _evaluate_float_group(
            group.get('rotation', {}), time, 'xyzw', unity_rot_default)
        # Some extracted Humanoid clips contain an all-zero RootQ placeholder,
        # which is not a valid quaternion. Treat it as an absent channel.
        if sum(component * component for component in rotation_value) < 1.0e-12:
            rotation_value = unity_rot_default
        body_rotation = Quaternion((
            rotation_value[3], rotation_value[0], rotation_value[1], rotation_value[2],
        )).normalized()
        if calibrate_bip001_root:
            avatar_position, avatar_rotation = _bip001_root_avatar_transform(
                muscle_curves, time, values_are_degrees)
            body_position = Vector(_evaluate_float_group(
                group.get('translation', {}), time, 'xyz', (0.0, 0.0, 0.0)))
            location = _unity_to_fbx_vector(
                body_position * _BIP001_ROOT_HUMAN_SCALE
                + body_rotation @ avatar_position
                + Vector((0.0, _BIP001_ROOT_HEIGHT_OFFSET, 0.0)))
            rotation = _unity_to_fbx_rotation(body_rotation @ avatar_rotation)
        else:
            rotation = _unity_to_fbx_rotation(body_rotation) @ source_rot
        matrices.append(Matrix.LocRotScale(location, rotation, Vector((1.0, 1.0, 1.0))))
    return matrices


def _write_matrix_channels(channelbag, target, matrices, frame_start, group_name):
    values_by_channel = [[] for _ in range(7)]
    previous_rotation = None
    for matrix in matrices:
        location, rotation, _scale = matrix.decompose()
        if previous_rotation is not None and previous_rotation.dot(rotation) < 0.0:
            rotation.negate()
        previous_rotation = rotation.copy()
        for values, value in zip(values_by_channel, (*location, rotation.w, rotation.x, rotation.y, rotation.z)):
            values.append(value)

    target.rotation_mode = 'QUATERNION'
    for data_path, size, offset in ((target.path_from_id('location'), 3, 0),
                                    (target.path_from_id('rotation_quaternion'), 4, 3)):
        for array_index in range(size):
            fcurve = channelbag.fcurves.find(data_path, index=array_index)
            if fcurve is None:
                fcurve = channelbag.fcurves.new(data_path, index=array_index, group_name=group_name)
            else:
                fcurve.keyframe_points.clear()
            fcurve.keyframe_points.add(len(matrices))
            coordinates = []
            for frame, value in enumerate(values_by_channel[offset + array_index]):
                coordinates.extend((frame_start + frame, value))
            fcurve.keyframe_points.foreach_set('co', coordinates)
            fcurve.update()


def import_clip(
        clip, armature, frame_start=1.0, root_motion='IGNORE',
        humanoid_preset=DEFAULT_HUMANOID_PRESET,
        use_bip001_avatar_calibration=False):
    preset = HUMANOID_PRESETS.get(humanoid_preset)
    if preset is None:
        raise ValueError(f"Unknown Humanoid mapping preset: {humanoid_preset}")
    use_bip001_avatar_calibration = (
        use_bip001_avatar_calibration
        and humanoid_preset == 'BIP001_ROOT_HIPS'
    )

    full_paths, suffix_paths = _bone_paths(armature)
    curve_groups = {}
    for kind, curves in (
            ('rotation', clip.rotation_curves), ('euler', clip.euler_curves),
            ('position', clip.position_curves), ('scale', clip.scale_curves)):
        for curve in curves:
            curve_groups.setdefault(curve.path, {})[kind] = curve

    mapped_by_bone = {}
    unresolved = []
    duplicate_paths = []
    for path, curves in curve_groups.items():
        bone_name = _resolve_bone(path, full_paths, suffix_paths, armature)
        if bone_name:
            existing = mapped_by_bone.get(bone_name)
            if existing is None:
                mapped_by_bone[bone_name] = [path, bone_name, dict(curves)]
            else:
                collision = False
                for kind, curve in curves.items():
                    if kind in existing[2]:
                        collision = True
                    else:
                        existing[2][kind] = curve
                if collision:
                    duplicate_paths.append(path)
        else:
            unresolved.append(path)
    mapped = list(mapped_by_bone.values())

    # Humanoid clips encode most body motion as named scalar "muscles", not as
    # per-bone Transform curves. Apply the built-in Bip001 calibration only to
    # matching bones and keep explicit Transform bindings authoritative.
    muscle_sources = [curve for curve in clip.float_curves if curve.attribute]
    float_groups = _float_curve_groups(clip)
    has_humanoid_payload = any(
        _muscle_attribute_alias(curve.attribute) in _MUSCLE_DEGREE_RANGES
        for curve in muscle_sources)
    humanoid_data_warning = ""
    if _has_misaligned_humanoid_data(muscle_sources, float_groups):
        humanoid_data_warning = (
            "Humanoid scalar curves appear misaligned/corrupt; body muscles and "
            "RootT/RootQ were ignored. Re-export or re-save the clip in Unity."
        )
        muscle_sources = []
    muscle_values_are_degrees = _muscle_values_are_degrees(muscle_sources)
    muscle_curves = {}
    for curve in muscle_sources:
        muscle_attribute = _muscle_attribute_alias(curve.attribute)
        curve_data = (curve, _filtered_muscle_values(curve, muscle_values_are_degrees))
        muscle_curves[curve.attribute] = curve_data
        muscle_curves[muscle_attribute] = curve_data
    if use_bip001_avatar_calibration and has_humanoid_payload:
        calibrated_mapped = []
        for path, bone_name, curves in mapped:
            curves = _without_constant_upper_arm_twist_channels(bone_name, curves)
            if curves:
                calibrated_mapped.append((path, bone_name, curves))
        mapped = calibrated_mapped
    native_bones = {bone_name for _path, bone_name, _curves in mapped}
    humanoid_count = 0
    for bone_name, profile in BIPED_HUMANOID_PROFILE.items():
        if bone_name not in armature.data.bones or bone_name in native_bones:
            continue
        if not any(attribute in muscle_curves for attribute, _minus, _plus in profile["effects"]):
            continue
        mapped.append(("Humanoid/" + bone_name, bone_name, {"humanoid": profile}))
        humanoid_count += 1

    # RootT/RootQ contain the Humanoid body-root pose.  Unity applies them to
    # whichever Transform the Avatar maps as Hips.  That Transform is not
    # necessarily the anatomical pelvis: some Bip001 FBX variants must map
    # Hips to the parent ``Bip001`` bone.  Keep this choice in the preset and
    # leave explicit Transform curves authoritative if the clip has one.
    humanoid_hips_bone = preset["hips_bone"]
    humanoid_root_group = {
        'translation': float_groups.get('RootT', {}),
        'rotation': float_groups.get('RootQ', {}),
    }
    has_humanoid_root = any(humanoid_root_group.values())
    humanoid_hips_applied = False
    if (humanoid_count
            and has_humanoid_root
            and humanoid_hips_bone in armature.data.bones
            and humanoid_hips_bone not in native_bones):
        mapped.append((
            "Humanoid/Hips",
            humanoid_hips_bone,
            {"humanoid_root": humanoid_root_group},
        ))
        native_bones.add(humanoid_hips_bone)
        humanoid_count += 1
        humanoid_hips_applied = True
    elif humanoid_count and has_humanoid_root and humanoid_hips_bone not in armature.data.bones:
        unresolved.append(f"Humanoid/Hips ({humanoid_hips_bone})")
    if not mapped:
        raise ValueError("No Unity Transform or supported Humanoid curves matched the selected armature")

    action = bpy.data.actions.new(clip.name)
    slot = action.slots.new(armature.id_type, "Slot")
    channelbag = anim_utils.action_ensure_channelbag_for_slot(action, slot)
    animation_data = armature.animation_data_create()
    animation_data.action = action
    animation_data.action_slot = slot

    fps = clip.sample_rate if clip.sample_rate > 0.0 else 60.0
    stop_time = clip.stop_time
    if stop_time is None:
        all_curves = (
            clip.rotation_curves + clip.euler_curves + clip.position_curves
            + clip.scale_curves + clip.float_curves
        )
        stop_time = max(key.time for curve in all_curves for key in curve.keys)
    frame_count = max(1, round((stop_time - clip.start_time) * fps)) + 1
    times = [clip.start_time + frame / fps for frame in range(frame_count)]

    corrections = _bone_retarget_corrections(armature)
    root_bone = None
    root_source_matrices = None
    root_source_deltas = None
    armature_root_matrices = None

    if root_motion != 'IGNORE':
        translation = float_groups.get('RootT') or float_groups.get('MotionT')
        rotation = float_groups.get('RootQ') or float_groups.get('MotionQ')
        if translation or rotation:
            root_bones = [bone for bone in armature.pose.bones if bone.parent is None]
            if len(root_bones) != 1:
                raise ValueError("Root Motion requires exactly one root bone")
            root_bone = root_bones[0]
            root_source_rest = _matrix_from_property(root_bone.bone.get("unity_fbx_source_rest"))
            if root_source_rest is None:
                root_source_rest = root_bone.bone.matrix_local.copy()
            root_source_matrices = _root_motion_source_matrices({
                'translation': translation or {},
                'rotation': rotation or {},
            }, times, root_source_rest, muscle_curves, muscle_values_are_degrees,
                humanoid_count > 0 and has_humanoid_root
                and use_bip001_avatar_calibration)
            root_source_initial_inv = root_source_matrices[0].inverted_safe()
            root_source_deltas = [matrix @ root_source_initial_inv for matrix in root_source_matrices]

            if root_motion == 'ARMATURE':
                baked_axis = _matrix_from_property(armature.get("unity_fbx_baked_axis_matrix"))
                if baked_axis is not None:
                    baked_axis_inv = baked_axis.inverted_safe()
                    armature_root_matrices = [
                        baked_axis @ delta @ baked_axis_inv for delta in root_source_deltas
                    ]
                else:
                    object_rest = armature.matrix_basis.copy()
                    armature_root_matrices = [object_rest @ delta for delta in root_source_deltas]

    for _path, bone_name, curves in mapped:
        pose_bone = armature.pose.bones[bone_name]
        pose_bone.rotation_mode = 'QUATERNION'
        source_rest = _matrix_from_property(pose_bone.bone.get("unity_fbx_source_rest"))
        if source_rest is None:
            source_rest = pose_bone.bone.matrix_local.copy()
            if pose_bone.parent:
                source_rest = pose_bone.parent.bone.matrix_local.inverted_safe() @ source_rest
        source_loc, source_rot, source_scale = source_rest.decompose()
        correction = corrections[bone_name]
        humanoid_root_matrices = None
        if curves.get('humanoid_root'):
            humanoid_root_matrices = _root_motion_source_matrices(
                curves['humanoid_root'], times, source_rest,
                muscle_curves, muscle_values_are_degrees,
                use_bip001_avatar_calibration)
        previous_rotation = None
        channel_values = [[] for _ in range(10)]

        for sample_index, time in enumerate(times):
            if humanoid_root_matrices is not None:
                source_local = humanoid_root_matrices[sample_index]
                position, rotation, scale = source_local.decompose()
            else:
                position_curve = curves.get('position')
                position = (_unity_to_fbx_vector(_evaluate_curve(
                    position_curve, time, 3, (0.0, 0.0, 0.0)))
                    if position_curve else source_loc)
                scale = _evaluate_curve(curves.get('scale'), time, 3, source_scale)
                if curves.get('humanoid'):
                    rotation = _evaluate_humanoid_rotation(
                        curves['humanoid'], muscle_curves, time, source_rot,
                        muscle_values_are_degrees)
                elif curves.get('rotation'):
                    rotation = _unity_to_fbx_quaternion(_evaluate_curve(
                        curves['rotation'], time, 4, (0.0, 0.0, 0.0, 1.0)))
                elif curves.get('euler'):
                    euler = _evaluate_curve(curves['euler'], time, 3, (0.0, 0.0, 0.0))
                    rotation = _unity_to_fbx_rotation(
                        Euler(tuple(math.radians(value) for value in euler), 'ZXY').to_quaternion())
                else:
                    rotation = source_rot
                source_local = Matrix.LocRotScale(position, rotation, Vector(scale))
            if root_motion == 'ARMATURE' and pose_bone == root_bone:
                # Move the root delta to the Armature object without applying it twice.
                source_local = root_source_deltas[sample_index].inverted_safe() @ source_local
            basis = correction.inverted_safe() @ source_rest.inverted_safe() @ source_local @ correction
            location, rotation, scale = basis.decompose()
            # Matrix decomposition may choose q or -q independently per frame.
            # Stabilize the quaternion actually written to the F-Curves, after
            # all rest-pose and bone-orientation changes have been applied.
            if previous_rotation is not None and previous_rotation.dot(rotation) < 0.0:
                rotation.negate()
            previous_rotation = rotation.copy()
            values = (*location, rotation.w, rotation.x, rotation.y, rotation.z, *scale)
            for values_for_channel, value in zip(channel_values, values):
                values_for_channel.append(value)

        paths = ((pose_bone.path_from_id("location"), 3),
                 (pose_bone.path_from_id("rotation_quaternion"), 4),
                 (pose_bone.path_from_id("scale"), 3))
        channel_index = 0
        for data_path, size in paths:
            for array_index in range(size):
                fcurve = channelbag.fcurves.new(data_path, index=array_index, group_name=bone_name)
                fcurve.keyframe_points.add(frame_count)
                coordinates = []
                for frame, value in enumerate(channel_values[channel_index]):
                    coordinates.extend((frame_start + frame, value))
                fcurve.keyframe_points.foreach_set('co', coordinates)
                fcurve.update()
                channel_index += 1

    if root_source_matrices is not None:
        if root_motion == 'ROOT_BONE':
            source_rest = _matrix_from_property(root_bone.bone.get("unity_fbx_source_rest"))
            if source_rest is None:
                source_rest = root_bone.bone.matrix_local.copy()
            correction = corrections[root_bone.name]
            root_basis_matrices = [
                correction.inverted_safe() @ source_rest.inverted_safe() @ matrix @ correction
                for matrix in root_source_matrices
            ]
            _write_matrix_channels(
                channelbag, root_bone, root_basis_matrices, frame_start, "Unity Root Motion")
        elif root_motion == 'ARMATURE':
            _write_matrix_channels(
                channelbag, armature, armature_root_matrices, frame_start, "Unity Root Motion")

    action["unity_source_sample_rate"] = clip.sample_rate
    action["unity_unresolved_paths"] = unresolved
    action["unity_duplicate_paths"] = duplicate_paths
    action["unity_root_motion"] = root_motion
    action["unity_humanoid_profile"] = preset["name"] if humanoid_count else ""
    action["unity_humanoid_preset"] = humanoid_preset if humanoid_count else ""
    action["unity_humanoid_hips_bone"] = humanoid_hips_bone if humanoid_hips_applied else ""
    action["unity_bip001_avatar_calibration"] = use_bip001_avatar_calibration
    action["unity_humanoid_warning"] = humanoid_data_warning
    action["unity_humanoid_bones"] = humanoid_count
    action["unity_humanoid_units"] = "degrees" if muscle_values_are_degrees else "normalized"
    return action, len(mapped), unresolved
