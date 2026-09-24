# SPDX-FileCopyrightText: 2011-2023 Blender Foundation
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bone collection and armature display helpers shared by the Unity FBX
importer and the 3D viewport Anim Utils panel."""

BIP_PREFIX = "bip"


def is_bip_bone(name):
    """True for Bip-named bones (Bip, Bip001, Bip001 Pelvis, ...)."""
    return name.lower().startswith(BIP_PREFIX)


def get_or_create_collection(arm_data, name):
    """Return the named bone collection, creating it when missing."""
    coll = arm_data.collections.get(name)
    if coll is None:
        coll = arm_data.collections.new(name)
    return coll


def move_bones_to_collection(arm_data, coll, bone_names):
    """Assign the named bones to `coll`, removing them from other collections."""
    for name in bone_names:
        bone = arm_data.bones.get(name)
        if bone is None:
            continue
        for other in list(arm_data.collections):
            if other != coll and name in other.bones:
                other.unassign(bone)
        if name not in coll.bones:
            coll.assign(bone)


def split_bip_collections(arm_obj):
    """Group the armature bones into a 'Bip' and a 'Non-Bip' bone collection.

    Returns (bip_collection, non_bip_collection); either is None when there
    are no bones of that group.
    """
    arm = arm_obj.data
    if arm is None:
        return None, None

    bip_names = [bone.name for bone in arm.bones if is_bip_bone(bone.name)]
    other_names = [bone.name for bone in arm.bones if not is_bip_bone(bone.name)]

    bip_coll = non_bip_coll = None
    if bip_names:
        bip_coll = get_or_create_collection(arm, "Bip")
        move_bones_to_collection(arm, bip_coll, bip_names)
    if other_names:
        non_bip_coll = get_or_create_collection(arm, "Non-Bip")
        move_bones_to_collection(arm, non_bip_coll, other_names)
    return bip_coll, non_bip_coll


def collect_matching_bones(arm_data, match_string):
    """Names of the bones containing `match_string` (case-insensitive)."""
    needle = match_string.lower()
    return [bone.name for bone in arm_data.bones if needle in bone.name.lower()]


def set_bone_color(bone, palette='DEFAULT', custom=None):
    """Set a bone color: a theme palette entry, or a custom RGB color."""
    bone.color.palette = palette
    if palette == 'CUSTOM' and custom is not None:
        rgb = tuple(custom)[:3]
        color = bone.color.custom
        color.normal = rgb
        color.select = rgb
        color.active = rgb


def apply_stick_display(arm_obj, display_type='STICK', show_in_front=True):
    """Set the armature display type and in-front drawing."""
    arm = arm_obj.data
    if arm is not None:
        arm.display_type = display_type
        arm.show_bone_colors = True
    arm_obj.show_in_front = show_in_front
