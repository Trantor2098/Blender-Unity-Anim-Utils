# SPDX-FileCopyrightText: 2011-2023 Blender Foundation
#
# SPDX-License-Identifier: GPL-2.0-or-later

bl_info = {
    "name": "Unity FBX & Animation Importer",
    "author": "Blender Foundation, Unity FBX contributors",
    "version": (0, 2, 0),
    "blender": (5, 0, 0),
    "location": "File > Import-Export; 3D Viewport > Sidebar > Unity Utils",
    "description": "Import Unity-oriented FBX files and Unity AnimationClip assets",
    "warning": "",
    "doc_url": "",
    "support": 'COMMUNITY',
    "category": "Import-Export",
}


if "bpy" in locals():
    import importlib
    if "import_fbx" in locals():
        importlib.reload(import_fbx)
    if "export_fbx_bin" in locals():
        importlib.reload(export_fbx_bin)
    if "export_fbx" in locals():
        importlib.reload(export_fbx)
    if "unity_anim" in locals():
        importlib.reload(unity_anim)
    if "unity_anim_action" in locals():
        importlib.reload(unity_anim_action)
    if "bone_collections" in locals():
        importlib.reload(bone_collections)


import bpy
from bpy.props import (
    StringProperty,
    BoolProperty,
    FloatProperty,
    FloatVectorProperty,
    EnumProperty,
    CollectionProperty,
    PointerProperty,
)
from bpy_extras.io_utils import (
    ImportHelper,
    ExportHelper,
    orientation_helper,
    path_reference_mode,
    axis_conversion,
    poll_file_object_drop,
)


@orientation_helper(axis_forward='-Z', axis_up='Y')
class ImportFBX(bpy.types.Operator, ImportHelper):
    """Load a FBX file"""
    bl_idname = "import_scene.unity_fbx"
    bl_label = "Import Unity FBX"
    bl_options = {'UNDO', 'PRESET'}

    directory: StringProperty(
        subtype='DIR_PATH',
        options={'HIDDEN', 'SKIP_PRESET'},
    )

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={'HIDDEN'})

    files: CollectionProperty(
        name="File Path",
        type=bpy.types.OperatorFileListElement,
        options={'HIDDEN', 'SKIP_PRESET'},
    )

    ui_tab: EnumProperty(
        items=(('MAIN', "Main", "Main basic settings"),
               ('ARMATURE', "Armatures", "Armature-related settings"),
               ),
        name="ui_tab",
        description="Import options categories",
    )

    use_manual_orientation: BoolProperty(
        name="Manual Orientation",
        description="Specify orientation and scale, instead of using embedded data in FBX file",
        default=False,
    )
    global_scale: FloatProperty(
        name="Scale",
        min=0.001, max=1000.0,
        default=1.0,
    )
    bake_space_transform: BoolProperty(
        name="Apply Transform",
        description="Bake space transform into object data, avoids getting unwanted rotations to objects when "
        "target space is not aligned with Blender's space "
        "(WARNING! experimental option, use at own risk, known to be broken with armatures/animations)",
        default=False,
    )
    bake_unity_axis: BoolProperty(
        name="Bake Axis Conversion",
        description="Bake the Y-up to Z-up conversion into the imported data so root objects keep an identity "
        "transform instead of a corrective 90 degree X rotation",
        default=True,
    )
    bake_unity_axis_all: BoolProperty(
        name="Bake Whole Hierarchy",
        description="Bake the axis conversion through the whole imported hierarchy instead of the root "
        "objects only, so nested meshes do not keep a residual local rotation that leaves "
        "the character visibly tilted",
        default=False,
    )

    use_custom_normals: BoolProperty(
        name="Custom Normals",
        description="Import custom normals, if available (otherwise Blender will recompute them)",
        default=True,
    )
    colors_type: EnumProperty(
        name="Vertex Colors",
        items=(('NONE', "None", "Do not import color attributes"),
               ('SRGB', "sRGB", "Expect file colors in sRGB color space"),
               ('LINEAR', "Linear", "Expect file colors in linear color space"),
               ),
        description="Import vertex color attributes",
        default='SRGB',
    )

    use_image_search: BoolProperty(
        name="Image Search",
        description="Search subdirs for any associated images (WARNING: may be slow)",
        default=True,
    )

    use_alpha_decals: BoolProperty(
        name="Alpha Decals",
        description="Treat materials with alpha as decals (no shadow casting)",
        default=False,
    )
    decal_offset: FloatProperty(
        name="Decal Offset",
        description="Displace geometry of alpha meshes",
        min=0.0, max=1.0,
        default=0.0,
    )

    use_anim: BoolProperty(
        name="Import Animation",
        description="Import FBX animation",
        default=True,
    )
    anim_offset: FloatProperty(
        name="Animation Offset",
        description="Offset to apply to animation during import, in frames",
        default=1.0,
    )
    import_companion_anim: BoolProperty(
        name="Import Companion Unity Animations",
        description="After the FBX import, import the Unity .anim clips found next to the FBX file "
        "onto the imported armature (one action per clip)",
        default=True,
    )
    anim_use_fake_user: BoolProperty(
        name="Fake User",
        description="Mark companion imported actions with a fake user so they are kept on save/reload",
        default=True,
    )
    anim_name_collision_mode: EnumProperty(
        name="Name Collision",
        description="Behavior when a companion imported action has the same name as an existing action",
        items=(
            ("OVERWRITE", "Overwrite", "Replace the curves of the existing same-named action"),
            ("REUSE", "Keep Existing", "Skip clips whose action name already exists"),
            ("RENAME", "Rename (Increment)", "Import as a new action with an incremented .001 suffix"),
        ),
        default='OVERWRITE',
    )
    anim_neutralize_root_offset: BoolProperty(
        name="Neutralize Root Offset",
        description=(
            "Remove the constant neutral offset of the Unity body-root pose of companion "
            "imported actions (RootT/RootQ pedestal or display offset); only the dynamic "
            "root motion is kept"
        ),
        default=False,
    )

    use_subsurf: BoolProperty(
        name="Subdivision Data",
        description="Import FBX subdivision information as subdivision surface modifiers",
        default=False,
    )

    use_custom_props: BoolProperty(
        name="Custom Properties",
        description="Import user properties as custom properties",
        default=True,
    )
    use_custom_props_enum_as_string: BoolProperty(
        name="Import Enums As Strings",
        description="Store enumeration values as strings",
        default=True,
    )

    ignore_leaf_bones: BoolProperty(
        name="Ignore Leaf Bones",
        description="Ignore the last bone at the end of each chain (used to mark the length of the previous bone)",
        default=False,
    )
    force_connect_children: BoolProperty(
        name="Force Connect Children",
        description="Force connection of children bones to their parent, even if their computed head/tail "
        "positions do not match (can be useful with pure-joints-type armatures)",
        default=False,
    )
    bone_orientation_mode: EnumProperty(
        name="Bone Orientation",
        items=(
            ('ORIGINAL', "FBX Original", "Keep the FBX bone axes"),
            ('BLENDER_AUTO', "Blender Automatic", "Align each bone to a dominant child axis"),
            ('BETTER_FBX', "Better FBX Style", "Point bones toward the average position of their children"),
        ),
        default='BETTER_FBX',
    )
    fbx_pose_mode: EnumProperty(
        name="Default Pose",
        description="How to handle the default pose stored in the FBX",
        items=(
            (
                'KEEP',
                "Keep",
                "Keep the FBX pose as imported",
            ),
            (
                'CLEAR',
                "Clear",
                "Clear pose transforms after import (imported animation actions are not removed)",
            ),
            (
                'ANIM_RETARGET',
                "Retarget Like Unity Anim",
                "Resolve the FBX default pose with the same rest-pose and bone-axis corrections as Unity .anim import",
            ),
        ),
        default='KEEP',
    )
    skin_bind_mode: EnumProperty(
        name="Skin Bind",
        description="How skinned-mesh bind data interacts with the mesh node transform. "
                    "Unity bakes the node transform into the skin bind matrices and then ignores it at "
                    "runtime, which offsets skinned meshes in Blender when the node transform is not identity",
        items=(
            (
                'UNITY',
                "Strip Node Transform (Unity)",
                "Strip the mesh node transform from the skin bind matrices, matching Unity runtime "
                "placement (fixes skinned meshes drifting away from the body)",
            ),
            (
                'FBX',
                "Keep Bind Data (FBX)",
                "Use the FBX bind matrices as-is, matching what other FBX importers do "
                "(skinned meshes follow their node transform)",
            ),
        ),
        default='UNITY',
    )
    import_bip_collections: BoolProperty(
        name="Bip/Non-Bip Bone Collections",
        description="After import, assign Bip-named bones (Bip001 ...) to a 'Bip' bone collection "
                    "and the remaining bones to a 'Non-Bip' bone collection",
        default=True,
    )
    import_stick_display: BoolProperty(
        name="Stick Display In Front",
        description="After import, set the armature display to Stick and enable In Front "
                    "for the imported armatures",
        default=True,
    )
    primary_bone_axis: EnumProperty(
        name="Primary Bone Axis",
        items=(('X', "X Axis", ""),
               ('Y', "Y Axis", ""),
               ('Z', "Z Axis", ""),
               ('-X', "-X Axis", ""),
               ('-Y', "-Y Axis", ""),
               ('-Z', "-Z Axis", ""),
               ),
        default='Y',
    )
    secondary_bone_axis: EnumProperty(
        name="Secondary Bone Axis",
        items=(('X', "X Axis", ""),
               ('Y', "Y Axis", ""),
               ('Z', "Z Axis", ""),
               ('-X', "-X Axis", ""),
               ('-Y', "-Y Axis", ""),
               ('-Z', "-Z Axis", ""),
               ),
        default='X',
    )

    use_prepost_rot: BoolProperty(
        name="Use Pre/Post Rotation",
        description="Use pre/post rotation from FBX transform (you may have to disable that in some cases)",
        default=True,
    )
    mtl_name_collision_mode: EnumProperty(
        name="Material Name Collision",
        items=(("MAKE_UNIQUE", "Make Unique", "Import each FBX material as a unique Blender material"),
               ("REFERENCE_EXISTING", "Reference Existing",
               "If a material with the same name already exists, reference that instead of importing"),
               ),
        default='MAKE_UNIQUE',
        description="Behavior when the name of an imported material conflicts with an existing material",
    )
    material_preset: EnumProperty(
        name="Material Preset",
        description="How the material node tree is generated from FBX data",
        items=(
            (
                "PRINCIPLED",
                "Principled BSDF (Full FBX)",
                "Generate a Principled BSDF from FBX data, connecting diffuse and normal maps, "
                "roughness, metalness and emission if present",
            ),
            (
                "PRINCIPLED_DEFAULT",
                "Principled BSDF (Default)",
                "Generate a default Principled BSDF, only connect the diffuse texture to it, "
                "and place the other imported texture nodes in the material node tree without connecting them",
            ),
            (
                "DIFFUSE",
                "Diffuse BSDF (Default)",
                "Same as the default Principled BSDF, but use a Diffuse BSDF instead",
            ),
            (
                "UNLIT",
                "Unlit",
                "Connect the diffuse texture straight to the material output, "
                "and place the other imported texture nodes without connecting them",
            ),
        ),
        default='PRINCIPLED_DEFAULT',
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False  # No animation.

        import_panel_include(layout, self)
        import_panel_transform(layout, self)
        import_panel_materials(layout, self)
        import_panel_animation(layout, self)
        import_panel_armature(layout, self)

    def execute(self, context):
        keywords = self.as_keywords(
            ignore=("filter_glob", "directory", "ui_tab", "filepath", "files",
                    "import_companion_anim", "anim_use_fake_user", "anim_name_collision_mode",
                    "anim_neutralize_root_offset",
                    "import_bip_collections", "import_stick_display"),
        )
        keywords["automatic_bone_orientation"] = self.bone_orientation_mode == 'BLENDER_AUTO'

        from . import import_fbx
        import os

        if self.files:
            paths = [os.path.join(self.directory, file.name) for file in self.files]
        else:
            paths = [self.filepath]

        # Recorded before the FBX import so only armatures created by this
        # very import are eligible targets for companion .anim clips.
        armature_names_before = {obj.name for obj in bpy.data.objects if obj.type == 'ARMATURE'}

        ret = {'CANCELLED'}
        imported_fbx = []
        for path in paths:
            if import_fbx.load(self, context, filepath=path, **keywords) == {'FINISHED'}:
                ret = {'FINISHED'}
                imported_fbx.append(path)

        if self.import_bip_collections or self.import_stick_display:
            self._post_import_armatures(
                context, armature_names_before,
                use_bip_collections=self.import_bip_collections,
                use_stick_display=self.import_stick_display)

        if self.import_companion_anim:
            self._import_companion_animations(
                context, imported_fbx, armature_names_before)

        return ret

    def _post_import_armatures(self, context, armature_names_before,
                               use_bip_collections=True, use_stick_display=True):
        """Apply bone collection and display options to the freshly imported armatures."""
        from . import bone_collections

        imported_armatures = [
            obj for obj in bpy.data.objects
            if obj.type == 'ARMATURE' and obj.name not in armature_names_before
        ]
        for arm_obj in imported_armatures:
            if use_bip_collections:
                bone_collections.split_bip_collections(arm_obj)
            if use_stick_display:
                bone_collections.apply_stick_display(arm_obj)

    def _import_companion_animations(self, context, fbx_paths, armature_names_before):
        """Import the Unity .anim clips found next to the imported FBX files."""
        import glob
        import os
        from . import unity_anim, unity_anim_action

        if not fbx_paths:
            return

        imported_armatures = [
            obj for obj in bpy.data.objects
            if obj.type == 'ARMATURE' and obj.name not in armature_names_before
        ]
        if not imported_armatures:
            return

        # Multiple FBX files may share a directory; import each clip once.
        seen = set()
        for fbx_path in fbx_paths:
            directory = os.path.dirname(fbx_path) or "."
            for anim_path in sorted(glob.glob(os.path.join(directory, "*.anim"))):
                if anim_path in seen:
                    continue
                seen.add(anim_path)
                try:
                    clip = unity_anim.load(anim_path)
                except (OSError, ValueError) as ex:
                    self.report({'ERROR'}, f"{os.path.basename(anim_path)}: {ex}")
                    continue

                action = None
                last_error = None
                for armature in imported_armatures:
                    try:
                        action, mapped_count, unresolved = unity_anim_action.import_clip(
                            clip,
                            armature,
                            1.0,
                            'IGNORE',
                            'BIP001_PELVIS_HIPS',
                            False,
                            use_fake_user=self.anim_use_fake_user,
                            name_collision_mode=self.anim_name_collision_mode,
                            neutralize_root_offset=self.anim_neutralize_root_offset,
                        )
                    except (OSError, ValueError) as ex:
                        last_error = str(ex)
                        continue
                    if action is not None:
                        message = (f"Imported {action.name} from {os.path.basename(anim_path)}: "
                                   f"{mapped_count} paths mapped, {len(unresolved)} unresolved")
                        warning = action.get("unity_humanoid_warning")
                        if warning:
                            message += f"; {warning}"
                        self.report({'WARNING'} if warning else {'INFO'}, message)
                        break
                if action is None:
                    if last_error:
                        self.report({'ERROR'}, f"{os.path.basename(anim_path)}: {last_error}")
                    else:
                        self.report({'WARNING'}, f"Skipped {os.path.basename(anim_path)}: "
                                  "an action with that name already exists")


def import_panel_include(layout, operator):
    header, body = layout.panel("UNITY_FBX_import_include", default_closed=False)
    header.label(text="Include")
    if body:
        body.prop(operator, "use_custom_normals")
        body.prop(operator, "use_subsurf")
        body.prop(operator, "use_custom_props")
        sub = body.row()
        sub.enabled = operator.use_custom_props
        sub.prop(operator, "use_custom_props_enum_as_string")
        body.prop(operator, "use_image_search")
        body.prop(operator, "colors_type")


def import_panel_transform(layout, operator):
    header, body = layout.panel("UNITY_FBX_import_transform", default_closed=False)
    header.label(text="Transform")
    if body:
        body.prop(operator, "global_scale")
        body.prop(operator, "decal_offset")
        row = body.row()
        row.prop(operator, "bake_space_transform")
        row.label(text="", icon='ERROR')
        body.prop(operator, "bake_unity_axis")
        sub = body.column()
        sub.enabled = operator.bake_unity_axis
        sub.prop(operator, "bake_unity_axis_all")
        body.prop(operator, "use_prepost_rot")

        import_panel_transform_orientation(body, operator)


def import_panel_transform_orientation(layout, operator):
    header, body = layout.panel("UNITY_FBX_import_transform_manual_orientation", default_closed=False)
    header.use_property_split = False
    header.prop(operator, "use_manual_orientation", text="")
    header.label(text="Manual Orientation")
    if body:
        body.enabled = operator.use_manual_orientation
        body.prop(operator, "axis_forward")
        body.prop(operator, "axis_up")


def import_panel_materials(layout, operator):
    header, body = layout.panel("UNITY_FBX_import_material", default_closed=True)
    header.label(text="Materials")
    if body:
        body.prop(operator, "material_preset")
        body.prop(operator, "mtl_name_collision_mode")


def import_panel_animation(layout, operator):
    header, body = layout.panel("UNITY_FBX_import_animation", default_closed=True)
    header.use_property_split = False
    header.prop(operator, "use_anim", text="")
    header.label(text="Animation")
    if body:
        body.enabled = operator.use_anim
        body.prop(operator, "anim_offset")
        body.prop(operator, "import_companion_anim")
        sub = body.column()
        sub.enabled = operator.import_companion_anim
        sub.prop(operator, "anim_use_fake_user")
        sub.prop(operator, "anim_name_collision_mode")
        sub.prop(operator, "anim_neutralize_root_offset")


def import_panel_armature(layout, operator):
    header, body = layout.panel("UNITY_FBX_import_armature", default_closed=True)
    header.label(text="Armature")
    if body:
        body.prop(operator, "ignore_leaf_bones")
        body.prop(operator, "force_connect_children"),
        body.prop(operator, "bone_orientation_mode")
        body.prop(operator, "fbx_pose_mode")
        body.prop(operator, "skin_bind_mode")
        body.prop(operator, "import_bip_collections")
        body.prop(operator, "import_stick_display")
        sub = body.column()
        sub.enabled = operator.bone_orientation_mode == 'ORIGINAL'
        sub.prop(operator, "primary_bone_axis")
        sub.prop(operator, "secondary_bone_axis")


class ImportUnityAnim(bpy.types.Operator, ImportHelper):
    """Import a Unity text AnimationClip onto the selected armature"""
    bl_idname = "import_anim.unity_anim"
    bl_label = "Import Unity Animation Clip"
    bl_options = {'UNDO'}

    # ImportHelper does not provide a `directory` property; without it the
    # fileselect would hand us a bare filename and os.path.join() would fail.
    directory: StringProperty(
        subtype='DIR_PATH',
        options={'HIDDEN', 'SKIP_PRESET'},
    )

    filename_ext = ".anim"
    filter_glob: StringProperty(default="*.anim", options={'HIDDEN'})

    files: CollectionProperty(
        name="File Path",
        type=bpy.types.OperatorFileListElement,
        options={'HIDDEN', 'SKIP_PRESET'},
    )

    frame_start: FloatProperty(
        name="Start Frame",
        default=1.0,
    )
    humanoid_preset: EnumProperty(
        name="Humanoid Mapping",
        description="Avatar bone mapping used to reconstruct Unity Humanoid muscle curves",
        items=(
            (
                'BIP001_PELVIS_HIPS',
                "Bip001 (Hips: Bip001 Pelvis)",
                "Common Bip001 Avatar mapping with Hips assigned to Bip001 Pelvis",
            ),
            (
                'BIP001_ROOT_HIPS',
                "Bip001 (Hips: Bip001)",
                "Bip001 variant used by FBX assets whose Unity Avatar requires Hips assigned to Bip001",
            ),
        ),
        default='BIP001_PELVIS_HIPS',
    )
    use_bip001_avatar_calibration: BoolProperty(
        name="GI Bip001 Avatar Calibration",
        description=(
            "Apply the model-specific Unity body-center, height, and constant "
            "upper-arm twist corrections calibrated for the GI Bip001 rig"
        ),
        default=False,
    )
    neutralize_root_offset: BoolProperty(
        name="Neutralize Root Offset",
        description=(
            "Remove the constant neutral offset of the Unity body-root pose "
            "(RootT/RootQ pedestal or display offset, e.g. UI standby clips "
            "sitting ~1 m up); only the dynamic root motion is kept"
        ),
        default=False,
    )
    root_motion: EnumProperty(
        name="Root Motion",
        items=(
            (
                'IGNORE',
                "Keep on Hips",
                "Keep Humanoid RootT/RootQ on the Hips bone without extracting separate root motion",
            ),
            ('ARMATURE', "Armature Object", "Extract root motion to the armature object and remove it from the root bone"),
            ('ROOT_BONE', "Root Bone", "Apply root motion to the single resolved root bone"),
        ),
        default='IGNORE',
    )
    use_fake_user: BoolProperty(
        name="Fake User",
        description="Mark imported actions with a fake user so they are kept when the file is saved and reloaded",
        default=True,
    )
    name_collision_mode: EnumProperty(
        name="Name Collision",
        description="Behavior when an imported action has the same name as an existing action",
        items=(
            (
                "OVERWRITE",
                "Overwrite",
                "Replace the curves of the existing same-named action",
            ),
            (
                "REUSE",
                "Keep Existing",
                "Skip importing clips whose action name already exists",
            ),
            (
                "RENAME",
                "Rename (Increment)",
                "Import as a new action with an incremented .001 suffix",
            ),
        ),
        default='OVERWRITE',
    )
    use_bone_collection_filter: BoolProperty(
        name="Limit to Bone Collection",
        description="Only import the curves of the bones in the chosen bone collection "
                    "(its child collections included); the other bones keep their "
                    "current animation",
        default=False,
    )
    bone_collection_filter: StringProperty(
        name="Bone Collection",
        description="Bone collection to restrict the imported curves to",
        default="",
    )
    patch_current_action: BoolProperty(
        name="Patch Current Action",
        description="With a bone collection filter, write the curves into the action "
                    "currently animating the armature instead of switching to a new "
                    "clip-named action (outside-collection bones keep their animation)",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False  # No animation.

        layout.prop(self, "frame_start")
        layout.prop(self, "humanoid_preset")
        layout.prop(self, "use_bip001_avatar_calibration")
        layout.prop(self, "neutralize_root_offset")
        layout.prop(self, "root_motion")
        layout.prop(self, "use_fake_user")
        layout.prop(self, "name_collision_mode")
        layout.separator()
        layout.use_property_split = False
        layout.prop(self, "use_bone_collection_filter")
        sub = layout.column()
        sub.enabled = self.use_bone_collection_filter
        sub.use_property_split = True
        # Offer the collections of the active armature as a dropdown.
        armature = context.active_object
        sub.prop_search(self, "bone_collection_filter", armature.data, "collections",
                        text="Bone Collection", icon='GROUP_BONE')
        sub.prop(self, "patch_current_action")

    def execute(self, context):
        import os
        from . import unity_anim, unity_anim_action

        armature = context.active_object

        if self.files:
            paths = [os.path.join(self.directory, file.name) for file in self.files]
        else:
            paths = [self.filepath]

        ret = {'CANCELLED'}
        imported = []
        failed = []
        skipped = []
        bone_collection_filter = None
        if self.use_bone_collection_filter and self.bone_collection_filter:
            bone_collection_filter = self.bone_collection_filter
            if bone_collection_filter not in armature.data.collections:
                self.report({'ERROR'},
                            f"Bone collection not found: {bone_collection_filter}")
                return {'CANCELLED'}
        for path in paths:
            try:
                clip = unity_anim.load(path)
                action, mapped_count, unresolved = unity_anim_action.import_clip(
                    clip,
                    armature,
                    self.frame_start,
                    self.root_motion,
                    self.humanoid_preset,
                    self.use_bip001_avatar_calibration,
                    use_fake_user=self.use_fake_user,
                    name_collision_mode=self.name_collision_mode,
                    neutralize_root_offset=self.neutralize_root_offset,
                    bone_collection_filter=bone_collection_filter,
                    patch_current_action=self.patch_current_action,
                )
            except (OSError, ValueError) as ex:
                failed.append(f"{os.path.basename(path)}: {ex}")
                continue

            if action is None:
                # Name collision with "Keep Existing": the existing action was kept as-is.
                skipped.append(os.path.basename(path))
                continue

            ret = {'FINISHED'}
            imported.append((action, mapped_count, unresolved))

        for action, mapped_count, unresolved in imported:
            message = f"Imported {action.name}: {mapped_count} paths mapped, {len(unresolved)} unresolved"
            filtered_out = action.get("unity_filtered_out_paths")
            if filtered_out:
                message += f", {len(filtered_out)} filtered out by bone collection"
            warning = action.get("unity_humanoid_warning")
            if warning:
                message += f"; {warning}"
            self.report({'WARNING'} if warning else {'INFO'}, message)
        for message in failed:
            self.report({'ERROR'}, message)
        for name in skipped:
            self.report({'WARNING'}, f"Skipped {name}: an action with that name already exists")

        return ret


@orientation_helper(axis_forward='-Z', axis_up='Y')
class ExportFBX(bpy.types.Operator, ExportHelper):
    """Write a FBX file"""
    bl_idname = "export_scene.fbx"
    bl_label = "Export FBX"
    bl_options = {'UNDO', 'PRESET'}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={'HIDDEN'})

    # List of operator properties, the attributes will be assigned
    # to the class instance from the operator settings before calling.

    use_selection: BoolProperty(
        name="Selected Objects",
        description="Export selected and visible objects only",
        default=False,
    )
    use_visible: BoolProperty(
        name='Visible Objects',
        description='Export visible objects only',
        default=False
    )
    use_active_collection: BoolProperty(
        name="Active Collection",
        description="Export only objects from the active collection (and its children)",
        default=False,
    )
    collection: StringProperty(
        name="Source Collection",
        description="Export only objects from this collection (and its children)",
        default="",
    )
    global_scale: FloatProperty(
        name="Scale",
        description="Scale all data (Some importers do not support scaled armatures!)",
        min=0.001, max=1000.0,
        soft_min=0.01, soft_max=1000.0,
        default=1.0,
    )
    apply_unit_scale: BoolProperty(
        name="Apply Unit",
        description=(
            "Take into account current Blender units settings "
            "(if unset, raw Blender Units values are used as-is)"
        ),
        default=True,
    )
    apply_scale_options: EnumProperty(
        items=(('FBX_SCALE_NONE', "All Local",
                "Apply custom scaling and units scaling to each object transformation, FBX scale remains at 1.0"),
               ('FBX_SCALE_UNITS', "FBX Units Scale",
                "Apply custom scaling to each object transformation, and units scaling to FBX scale"),
               ('FBX_SCALE_CUSTOM', "FBX Custom Scale",
                "Apply custom scaling to FBX scale, and units scaling to each object transformation"),
               ('FBX_SCALE_ALL', "FBX All",
                "Apply custom scaling and units scaling to FBX scale"),
               ),
        name="Apply Scalings",
        description="How to apply custom and units scalings in generated FBX file "
        "(Blender uses FBX scale to detect units on import, "
        "but many other applications do not handle the same way)",
    )

    use_space_transform: BoolProperty(
        name="Use Space Transform",
        description="Apply global space transform to the object rotations. When disabled "
        "only the axis space is written to the file and all object transforms are left as-is",
        default=True,
    )
    bake_space_transform: BoolProperty(
        name="Apply Transform",
        description="Bake space transform into object data, avoids getting unwanted rotations to objects when "
        "target space is not aligned with Blender's space "
        "(WARNING! experimental option, use at own risk, known to be broken with armatures/animations)",
        default=False,
    )

    object_types: EnumProperty(
        name="Object Types",
        options={'ENUM_FLAG'},
        items=(('EMPTY', "Empty", ""),
               ('CAMERA', "Camera", ""),
               ('LIGHT', "Lamp", ""),
               ('ARMATURE', "Armature", "WARNING: not supported in dupli/group instances"),
               ('MESH', "Mesh", ""),
               ('OTHER', "Other", "Other geometry types, like curve, meta-ball, etc. (converted to meshes)"),
               ),
        description="Which kind of object to export",
        default={'EMPTY', 'CAMERA', 'LIGHT', 'ARMATURE', 'MESH', 'OTHER'},
    )

    use_mesh_modifiers: BoolProperty(
        name="Apply Modifiers",
        description="Apply modifiers to mesh objects (except Armature ones) - "
        "WARNING: prevents exporting shape keys",
        default=True,
    )
    use_mesh_modifiers_render: BoolProperty(
        name="Use Modifiers Render Setting",
        description="Use render settings when applying modifiers to mesh objects (DISABLED in Blender 2.8)",
        default=True,
    )
    mesh_smooth_type: EnumProperty(
        name="Smoothing",
        items=(('OFF', "Normals Only", "Export only normals instead of writing edge or face smoothing data"),
               ('FACE', "Face", "Write face smoothing"),
               ('EDGE', "Edge", "Write edge smoothing"),
               ('SMOOTH_GROUP', "Smoothing Groups", "Write face smoothing groups"),
               ),
        description="Export smoothing information "
        "(prefer 'Normals Only' option if your target importer understands custom normals)",
        default='OFF',
    )
    colors_type: EnumProperty(
        name="Vertex Colors",
        items=(('NONE', "None", "Do not export color attributes"),
               ('SRGB', "sRGB", "Export colors in sRGB color space"),
               ('LINEAR', "Linear", "Export colors in linear color space"),
               ),
        description="Export vertex color attributes",
        default='SRGB',
    )
    prioritize_active_color: BoolProperty(
        name="Prioritize Active Color",
        description="Make sure active color will be exported first. Could be important "
        "since some other software can discard other color attributes besides the first one",
        default=False,
    )
    use_subsurf: BoolProperty(
        name="Export Subdivision Surface",
        description="Export the last Catmull-Rom subdivision modifier as FBX subdivision "
        "(does not apply the modifier even if 'Apply Modifiers' is enabled)",
        default=False,
    )
    use_mesh_edges: BoolProperty(
        name="Loose Edges",
        description="Export loose edges (as two-vertices polygons)",
        default=False,
    )
    use_tspace: BoolProperty(
        name="Tangent Space",
        description="Add binormal and tangent vectors, together with normal they form the tangent space "
        "(will only work correctly with tris/quads only meshes!)",
        default=False,
    )
    use_triangles: BoolProperty(
        name="Triangulate Faces",
        description="Convert all faces to triangles",
        default=False,
    )
    use_custom_props: BoolProperty(
        name="Custom Properties",
        description="Export custom properties",
        default=False,
    )
    add_leaf_bones: BoolProperty(
        name="Add Leaf Bones",
        description="Append a final bone to the end of each chain to specify last bone length "
        "(use this when you intend to edit the armature from exported data)",
        default=True  # False for commit!
    )
    primary_bone_axis: EnumProperty(
        name="Primary Bone Axis",
        items=(('X', "X Axis", ""),
               ('Y', "Y Axis", ""),
               ('Z', "Z Axis", ""),
               ('-X', "-X Axis", ""),
               ('-Y', "-Y Axis", ""),
               ('-Z', "-Z Axis", ""),
               ),
        default='Y',
    )
    secondary_bone_axis: EnumProperty(
        name="Secondary Bone Axis",
        items=(('X', "X Axis", ""),
               ('Y', "Y Axis", ""),
               ('Z', "Z Axis", ""),
               ('-X', "-X Axis", ""),
               ('-Y', "-Y Axis", ""),
               ('-Z', "-Z Axis", ""),
               ),
        default='X',
    )
    use_armature_deform_only: BoolProperty(
        name="Only Deform Bones",
        description="Only write deforming bones (and non-deforming ones when they have deforming children)",
        default=False,
    )
    armature_nodetype: EnumProperty(
        name="Armature FBXNode Type",
        items=(('NULL', "Null", "'Null' FBX node, similar to Blender's Empty (default)"),
               ('ROOT', "Root", "'Root' FBX node, supposed to be the root of chains of bones..."),
               ('LIMBNODE', "LimbNode", "'LimbNode' FBX node, a regular joint between two bones..."),
               ),
        description="FBX type of node (object) used to represent Blender's armatures "
        "(use the Null type unless you experience issues with the other app, "
        "as other choices may not import back perfectly into Blender...)",
        default='NULL',
    )
    bake_anim: BoolProperty(
        name="Baked Animation",
        description="Export baked keyframe animation",
        default=True,
    )
    bake_anim_use_all_bones: BoolProperty(
        name="Key All Bones",
        description="Force exporting at least one key of animation for all bones "
        "(needed with some target applications, like UE4)",
        default=True,
    )
    bake_anim_use_nla_strips: BoolProperty(
        name="NLA Strips",
        description="Export each non-muted NLA strip as a separated FBX's AnimStack, if any, "
        "instead of global scene animation",
        default=True,
    )
    bake_anim_use_all_actions: BoolProperty(
        name="All Actions",
        description="Export each action as a separated FBX's AnimStack, instead of global scene animation "
        "(note that animated objects will get all actions compatible with them, "
        "others will get no animation at all)",
        default=True,
    )
    bake_anim_force_startend_keying: BoolProperty(
        name="Force Start/End Keying",
        description="Always add a keyframe at start and end of actions for animated channels",
        default=True,
    )
    bake_anim_step: FloatProperty(
        name="Sampling Rate",
        description="How often to evaluate animated values (in frames)",
        min=0.01, max=100.0,
        soft_min=0.1, soft_max=10.0,
        default=1.0,
    )
    bake_anim_simplify_factor: FloatProperty(
        name="Simplify",
        description="How much to simplify baked values (0.0 to disable, the higher the more simplified)",
        min=0.0, max=100.0,  # No simplification to up to 10% of current magnitude tolerance.
        soft_min=0.0, soft_max=10.0,
        default=1.0,  # default: min slope: 0.005, max frame step: 10.
    )
    path_mode: path_reference_mode
    embed_textures: BoolProperty(
        name="Embed Textures",
        description="Embed textures in FBX binary file (only for \"Copy\" path mode!)",
        default=False,
    )
    batch_mode: EnumProperty(
        name="Batch Mode",
        items=(('OFF', "Off", "Active scene to file"),
               ('SCENE', "Scene", "Each scene as a file"),
               ('COLLECTION', "Collection",
                "Each collection (data-block ones) as a file, does not include content of children collections"),
               ('SCENE_COLLECTION', "Scene Collections",
                "Each collection (including master, non-data-block ones) of each scene as a file, "
                "including content from children collections"),
               ('ACTIVE_SCENE_COLLECTION', "Active Scene Collections",
                "Each collection (including master, non-data-block one) of the active scene as a file, "
                "including content from children collections"),
               ),
    )
    use_batch_own_dir: BoolProperty(
        name="Batch Own Dir",
        description="Create a dir for each exported file",
        default=True,
    )
    use_metadata: BoolProperty(
        name="Use Metadata",
        default=True,
        options={'HIDDEN'},
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False  # No animation.

        # Are we inside the File browser
        is_file_browser = context.space_data.type == 'FILE_BROWSER'

        export_main(layout, self, is_file_browser)
        export_panel_include(layout, self, is_file_browser)
        export_panel_transform(layout, self)
        export_panel_geometry(layout, self)
        export_panel_armature(layout, self)
        export_panel_animation(layout, self)

    @property
    def check_extension(self):
        return self.batch_mode == 'OFF'

    def execute(self, context):
        from mathutils import Matrix
        if not self.filepath:
            raise Exception("filepath not set")

        global_matrix = (axis_conversion(to_forward=self.axis_forward,
                                         to_up=self.axis_up,
                                         ).to_4x4()
                         if self.use_space_transform else Matrix())

        keywords = self.as_keywords(ignore=("check_existing",
                                            "filter_glob",
                                            "ui_tab",
                                            ))

        keywords["global_matrix"] = global_matrix

        from . import export_fbx_bin
        return export_fbx_bin.save(self, context, **keywords)


def export_main(layout, operator, is_file_browser):
    row = layout.row(align=True)
    row.prop(operator, "path_mode")
    sub = row.row(align=True)
    sub.enabled = (operator.path_mode == 'COPY')
    sub.prop(operator, "embed_textures", text="", icon='PACKAGE' if operator.embed_textures else 'UGLYPACKAGE')
    if is_file_browser:
        row = layout.row(align=True)
        row.prop(operator, "batch_mode")
        sub = row.row(align=True)
        sub.prop(operator, "use_batch_own_dir", text="", icon='NEWFOLDER')


def export_panel_include(layout, operator, is_file_browser):
    header, body = layout.panel("FBX_export_include", default_closed=False)
    header.label(text="Include")
    if body:
        sublayout = body.column(heading="Limit to")
        sublayout.enabled = (operator.batch_mode == 'OFF')
        if is_file_browser:
            sublayout.prop(operator, "use_selection")
            sublayout.prop(operator, "use_visible")
            sublayout.prop(operator, "use_active_collection")

        body.column().prop(operator, "object_types")
        body.prop(operator, "use_custom_props")


def export_panel_transform(layout, operator):
    header, body = layout.panel("FBX_export_transform", default_closed=False)
    header.label(text="Transform")
    if body:
        body.prop(operator, "global_scale")
        body.prop(operator, "apply_scale_options")

        body.prop(operator, "axis_forward")
        body.prop(operator, "axis_up")

        body.prop(operator, "apply_unit_scale")
        body.prop(operator, "use_space_transform")
        row = body.row()
        row.prop(operator, "bake_space_transform")
        row.label(text="", icon='ERROR')


def export_panel_geometry(layout, operator):
    header, body = layout.panel("FBX_export_geometry", default_closed=True)
    header.label(text="Geometry")
    if body:
        body.prop(operator, "mesh_smooth_type")
        body.prop(operator, "use_subsurf")
        body.prop(operator, "use_mesh_modifiers")
        # sub = body.row()
        # sub.enabled = operator.use_mesh_modifiers and False  # disabled in 2.8...
        # sub.prop(operator, "use_mesh_modifiers_render")
        body.prop(operator, "use_mesh_edges")
        body.prop(operator, "use_triangles")
        sub = body.row()
        # ~ sub.enabled = operator.mesh_smooth_type in {'OFF'}
        sub.prop(operator, "use_tspace")
        body.prop(operator, "colors_type")
        body.prop(operator, "prioritize_active_color")


def export_panel_armature(layout, operator):
    header, body = layout.panel("FBX_export_armature", default_closed=True)
    header.label(text="Armature")
    if body:
        body.prop(operator, "primary_bone_axis")
        body.prop(operator, "secondary_bone_axis")
        body.prop(operator, "armature_nodetype")
        body.prop(operator, "use_armature_deform_only")
        body.prop(operator, "add_leaf_bones")


def export_panel_animation(layout, operator):
    header, body = layout.panel("FBX_export_bake_animation", default_closed=True)
    header.use_property_split = False
    header.prop(operator, "bake_anim", text="")
    header.label(text="Animation")
    if body:
        body.enabled = operator.bake_anim
        body.prop(operator, "bake_anim_use_all_bones")
        body.prop(operator, "bake_anim_use_nla_strips")
        body.prop(operator, "bake_anim_use_all_actions")
        body.prop(operator, "bake_anim_force_startend_keying")
        body.prop(operator, "bake_anim_step")
        body.prop(operator, "bake_anim_simplify_factor")


def _merge_collection_items(self, context):
    """Bone collections of the active armature for the merge multi-select enum."""
    items = []
    obj = getattr(context, "active_object", None) if context else None
    if obj is not None and getattr(obj, "type", None) == 'ARMATURE':
        for collection in obj.data.collections:
            items.append((collection.name, collection.name, ""))
    if not items:
        # ENUM_FLAG properties need at least one item to register.
        items.append(("__none__", "No Collections", ""))
    return items


PALETTE_ITEMS = (
    ('DEFAULT', "Default", "Default theme color"),
    ('THEME01', "Theme 01", ""), ('THEME02', "Theme 02", ""),
    ('THEME03', "Theme 03", ""), ('THEME04', "Theme 04", ""),
    ('THEME05', "Theme 05", ""), ('THEME06', "Theme 06", ""),
    ('THEME07', "Theme 07", ""), ('THEME08', "Theme 08", ""),
    ('THEME09', "Theme 09", ""), ('THEME10', "Theme 10", ""),
    ('THEME11', "Theme 11", ""), ('THEME12', "Theme 12", ""),
    ('THEME13', "Theme 13", ""), ('THEME14', "Theme 14", ""),
    ('THEME15', "Theme 15", ""), ('THEME16', "Theme 16", ""),
    ('THEME17', "Theme 17", ""), ('THEME18', "Theme 18", ""),
    ('THEME19', "Theme 19", ""), ('THEME20', "Theme 20", ""),
    ('CUSTOM', "Custom", "Custom RGB color"),
)


class UNITY_FBX_OT_bone_collection_from_name(bpy.types.Operator):
    """Collect the bones whose name contains the given string into a bone collection"""
    bl_idname = "unity_fbx.bone_collection_from_name"
    bl_label = "Create Bone Collection From Name"
    bl_options = {'UNDO', 'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE'

    def execute(self, context):
        from . import bone_collections

        settings = context.scene.unity_fbx_anim_utils
        arm_obj = context.active_object
        arm = arm_obj.data

        bone_names = bone_collections.collect_matching_bones(arm, settings.match_string)
        if not bone_names:
            self.report({'WARNING'}, f"No bone contains {settings.match_string!r}")
            return {'CANCELLED'}

        coll = bone_collections.get_or_create_collection(arm, settings.collection_name)
        bone_collections.move_bones_to_collection(arm, coll, bone_names)

        for name in bone_names:
            bone = arm.bones[name]
            if settings.use_bone_color:
                if settings.bone_color_mode == 'CUSTOM':
                    bone_collections.set_bone_color(bone, 'CUSTOM', settings.custom_color)
                else:
                    bone_collections.set_bone_color(bone, settings.bone_color_palette)

        if settings.set_display_as:
            # Display As is an armature-data setting in Blender.
            arm.display_type = settings.display_as
            arm.show_bone_colors = True

        self.report({'INFO'}, f"{coll.name}: {len(bone_names)} bones")
        return {'FINISHED'}


def menu_func_import(self, context):
    self.layout.operator(ImportFBX.bl_idname, text="Unity FBX (.fbx)")
    self.layout.operator(ImportUnityAnim.bl_idname, text="Unity Animation Clip (.anim)")


def menu_func_export(self, context):
    self.layout.operator(ExportFBX.bl_idname, text="FBX (.fbx)")


class UNITY_FBX_OT_merge_actions_by_collection(bpy.types.Operator):
    """Override the base action with the override action's curves of the selected bone collections"""
    bl_idname = "unity_fbx.merge_actions_by_collection"
    bl_label = "Override Base Action By Collection"
    bl_options = {'UNDO', 'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE'

    def execute(self, context):
        from . import unity_anim_action

        settings = context.scene.unity_fbx_anim_utils
        armature = context.active_object

        base = settings.merge_base_action
        override = settings.merge_override_action
        if base is None or override is None:
            self.report({'ERROR'}, "Select a base and an override action")
            return {'CANCELLED'}
        if base == override:
            self.report({'ERROR'}, "Base and override actions must differ")
            return {'CANCELLED'}
        collections = sorted(settings.merge_collections)
        if not collections:
            self.report({'ERROR'}, "Select at least one bone collection")
            return {'CANCELLED'}

        try:
            action, merged, skipped = unity_anim_action.merge_actions_by_collection(
                armature, [override], base.name,
                collections, use_fake_user=settings.merge_use_fake_user)
        except (OSError, ValueError) as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}

        message = (f"{override.name} -> {action.name}: {merged} fcurves overridden"
                   f" within {', '.join(collections)}, {skipped} kept/skipped")
        self.report({'INFO'}, message)
        return {'FINISHED'}


# --- 3D Viewport GUI Panel -------------------------------------------------

class UnityFBXAnimUtilsSettings(bpy.types.PropertyGroup):
    """Settings for the Anim Utils bone collection operator."""
    match_string: StringProperty(
        name="Match",
        description="Bones whose name contains this string are collected",
        default="Bip",
    )
    collection_name: StringProperty(
        name="Collection",
        description="Name of the bone collection to create or reuse",
        default="Bip",
    )
    use_bone_color: BoolProperty(
        name="Set Bone Color",
        description="Set the bone color of the collected bones",
        default=True,
    )
    bone_color_mode: EnumProperty(
        name="Color",
        description="Bone color source",
        items=(
            ('PALETTE', "Theme Palette", "Use a theme palette color"),
            ('CUSTOM', "Custom", "Use a custom RGB color"),
        ),
        default='PALETTE',
    )
    bone_color_palette: EnumProperty(
        name="Palette",
        description="Theme palette color",
        items=PALETTE_ITEMS,
        default='THEME01',
    )
    custom_color: FloatVectorProperty(
        name="Custom Color",
        description="Custom bone color",
        subtype='COLOR',
        size=4,
        min=0.0, max=1.0,
        default=(1.0, 0.3, 0.0, 1.0),
    )
    set_display_as: BoolProperty(
        name="Set Display As",
        description="Set the display style of the collected bones",
        default=False,
    )
    display_as: EnumProperty(
        name="Display As",
        description="Bone display style in the viewport",
        items=(
            ('OCTAHEDRAL', "Octahedral", ""),
            ('STICK', "Stick", ""),
            ('BBONE', "B-Bone", ""),
            ('ENVELOPE', "Envelope", ""),
            ('WIRE', "Wire", ""),
        ),
        default='STICK',
    )
    # --- Merge Actions By Collection ---
    merge_base_action: PointerProperty(
        type=bpy.types.Action,
        name="Base Action",
        description="Action to override: keeps its curves except where the "
                    "override action animates a bone of the selected collections",
    )
    merge_override_action: PointerProperty(
        type=bpy.types.Action,
        name="Override Action",
        description="Action whose curves of the selected bone collections "
                    "override the base action",
    )
    merge_collections: EnumProperty(
        name="Collections",
        description="Bone collections whose curves are taken from the override action",
        options={'ENUM_FLAG'},
        items=_merge_collection_items,
    )
    merge_use_fake_user: BoolProperty(
        name="Fake User",
        description="Mark the merged action with a fake user",
        default=True,
    )


class UNITY_FBX_PT_viewport_anim_utils(bpy.types.Panel):
    bl_label = "Anim Utils"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Unity Utils"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        settings = context.scene.unity_fbx_anim_utils

        # --- Bone Collection From Name (collapsible sub-panel) ---
        header, body = layout.panel("UNITY_FBX_bone_collection_from_name",
                                    default_closed=False)
        header.label(text="Bone Collection From Name", icon='GROUP_BONE')
        if body:
            body.prop(settings, "match_string")
            body.prop(settings, "collection_name")
            body.separator()
            sub = body.column()
            sub.use_property_split = False
            sub.prop(settings, "use_bone_color")
            color_col = sub.column()
            color_col.enabled = settings.use_bone_color
            color_col.use_property_split = True
            color_col.prop(settings, "bone_color_mode", text="")
            if settings.bone_color_mode == 'PALETTE':
                color_col.prop(settings, "bone_color_palette", text="Palette")
            else:
                color_col.prop(settings, "custom_color", text="")
            body.separator()
            sub2 = body.column()
            sub2.use_property_split = False
            sub2.prop(settings, "set_display_as")
            display_col = sub2.column()
            display_col.enabled = settings.set_display_as
            display_col.use_property_split = True
            display_col.prop(settings, "display_as", text="Style")
            body.separator()
            body.operator(UNITY_FBX_OT_bone_collection_from_name.bl_idname,
                          icon='GROUP_BONE')

        layout.separator()

        # --- Override Base Action By Collection (collapsible sub-panel) ---
        header, body = layout.panel("UNITY_FBX_merge_actions", default_closed=True)
        header.label(text="Override Base Action By Collection", icon='ACTION')
        if body:
            body.prop(settings, "merge_base_action")
            body.prop(settings, "merge_override_action")
            body.prop(settings, "merge_collections")
            body.prop(settings, "merge_use_fake_user")
            body.separator()
            body.operator(UNITY_FBX_OT_merge_actions_by_collection.bl_idname,
                          icon='ACTION')


class UNITY_FBX_PT_viewport_import(bpy.types.Panel):
    bl_label = "Import"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Unity Utils"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        col = layout.column()
        col.operator(ImportFBX.bl_idname, text="Unity FBX (.fbx)", icon='IMPORT')
        col.operator(ImportUnityAnim.bl_idname, text="Unity Animation Clip (.anim)",
                     icon='ANIM')


classes = (
    ImportFBX,
    ImportUnityAnim,
    UnityFBXAnimUtilsSettings,
    UNITY_FBX_OT_bone_collection_from_name,
    UNITY_FBX_OT_merge_actions_by_collection,
    UNITY_FBX_PT_viewport_anim_utils,
    UNITY_FBX_PT_viewport_import,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.unity_fbx_anim_utils = PointerProperty(
        type=UnityFBXAnimUtilsSettings)

    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)

    if hasattr(bpy.types.Scene, "unity_fbx_anim_utils"):
        del bpy.types.Scene.unity_fbx_anim_utils

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
