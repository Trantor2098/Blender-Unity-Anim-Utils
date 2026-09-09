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


def _evaluate_humanoid_rotation(
        profile, muscle_curves, time, source_rest_rotation, values_are_degrees):
    """Evaluate calibrated muscle deltas around this FBX bone's own rest rotation."""
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
        factor = abs(value)
        if values_are_degrees:
            degree_range = _MUSCLE_DEGREE_RANGES[attribute][1 if value >= 0.0 else 0]
            if degree_range > 1.0e-8:
                factor /= degree_range
        if side == "left":
            delta = endpoint @ base_inverse
            rotation = _scaled_quaternion_delta(delta, factor) @ rotation
        else:
            delta = base_inverse @ endpoint
            rotation = rotation @ _scaled_quaternion_delta(delta, factor)
    # Retarget the calibrated Avatar's absolute local rotation A onto the target
    # rest T with C = inverse(S) * T, hence A_target = A * C. This is deliberately
    # not T * inverse(S) * A: that order rotates around the calibration bone axes
    # and twists targets whose FBX local axes differ from the calibration Avatar.
    target_rest_unity = _unity_to_fbx_rotation(source_rest_rotation)
    rest_correction = base_inverse @ target_rest_unity
    return _unity_to_fbx_rotation(rotation @ rest_correction).normalized()


def _muscle_attribute_alias(attribute):
    """Translate serialized finger names to Unity's HumanTrait muscle names."""
    for side in ("Left", "Right"):
        prefix = side + "Hand."
        if attribute.startswith(prefix):
            return side + " " + attribute[len(prefix):].replace('.', ' ')
    return attribute


def _filtered_muscle_values(curve, values_are_degrees=False):
    """Remove isolated corrupt samples from dense serialized Humanoid curves.

    Some exported text clips contain one-frame values tens of times outside their
    neighboring muscle samples. A centered median leaves monotonic motion intact
    while replacing only conspicuous local spikes.
    """
    values = [float(key.value) for key in curve.keys]
    if len(values) < 3:
        return values

    # Humanoid muscles are normalized values. Moderate overshoot is allowed, but
    # values such as 10, 34 or 84 in the sample are corrupted single-frame data.
    # Reconstruct those runs from the nearest credible samples first.
    plausible_limit = 180.0 if values_are_degrees else 2.0
    valid_indices = [index for index, value in enumerate(values)
                     if math.isfinite(value) and abs(value) <= plausible_limit]
    if valid_indices:
        for index, value in enumerate(values):
            if math.isfinite(value) and abs(value) <= plausible_limit:
                continue
            left = next((candidate for candidate in reversed(valid_indices) if candidate < index), None)
            right = next((candidate for candidate in valid_indices if candidate > index), None)
            if left is None:
                values[index] = values[right]
            elif right is None:
                values[index] = values[left]
            else:
                factor = (index - left) / (right - left)
                values[index] = values[left] + (values[right] - values[left]) * factor

    # Three median passes handle the alternating/two-sample corruption pattern
    # present in the GI Humanoid export while preserving monotonic motion.
    filtered = values
    for _pass in range(3):
        source = filtered
        filtered = []
        for index in range(len(source)):
            start = max(0, index - 3)
            stop = min(len(source), index + 4)
            window = sorted(source[start:stop])
            middle = len(window) // 2
            median = (window[middle] if len(window) % 2
                      else (window[middle - 1] + window[middle]) * 0.5)
            filtered.append(median)

    # A short symmetric average removes the remaining staircase edges without
    # introducing a temporal phase shift.
    smoothed = []
    for index in range(len(filtered)):
        start = max(0, index - 2)
        stop = min(len(filtered), index + 3)
        smoothed.append(sum(filtered[start:stop]) / (stop - start))
    return smoothed


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


def _root_motion_source_matrices(group, times, source_rest):
    source_loc, source_rot, _source_scale = source_rest.decompose()
    unity_loc_default = (-source_loc.x, source_loc.y, source_loc.z)
    unity_rot_default = (source_rot.x, -source_rot.y, -source_rot.z, source_rot.w)
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
        rotation = _unity_to_fbx_quaternion(rotation_value)
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
        humanoid_preset=DEFAULT_HUMANOID_PRESET):
    preset = HUMANOID_PRESETS.get(humanoid_preset)
    if preset is None:
        raise ValueError(f"Unknown Humanoid mapping preset: {humanoid_preset}")

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
    muscle_values_are_degrees = any(
        _muscle_attribute_alias(curve.attribute) in _MUSCLE_DEGREE_RANGES
        and any(abs(float(key.value)) > 2.0 for key in curve.keys)
        for curve in muscle_sources)
    muscle_curves = {}
    for curve in muscle_sources:
        muscle_attribute = _muscle_attribute_alias(curve.attribute)
        curve_data = (curve, _filtered_muscle_values(curve, muscle_values_are_degrees))
        muscle_curves[curve.attribute] = curve_data
        muscle_curves[muscle_attribute] = curve_data
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
    float_groups = _float_curve_groups(clip)
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
            }, times, root_source_rest)
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
                curves['humanoid_root'], times, source_rest)
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
    action["unity_humanoid_bones"] = humanoid_count
    action["unity_humanoid_units"] = "degrees" if muscle_values_are_degrees else "normalized"
    return action, len(mapped), unresolved
