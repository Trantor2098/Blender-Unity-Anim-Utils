# Unity FBX 与 AnimationClip 导入实现说明

本文记录本插件相对 Blender FBX 导入器增加的功能、坐标空间约定、动画重定向公式，以及开发过程中遇到的主要坑点。目标是让后续维护者能够修改代码，而不会再次引入骨骼躺倒、网格拉伸或局部骨骼反折问题。

## 1. 功能概览

插件提供两类导入操作：

1. 导入 Unity 工程使用的 FBX 模型。
2. 将 Unity 文本格式 `.anim` AnimationClip 导入为选中骨架的 Blender Action。

FBX 导入部分在 Blender 官方导入器基础上增加了：

- `Bake Axis Conversion`：把 Unity/FBX 的 Y-up 到 Blender Z-up 转换烘焙进数据，使根对象保持单位变换。
- `Bone Orientation`：支持保留 FBX 原始骨轴、Blender Automatic 和 Better FBX 风格三种骨骼朝向。
- 保存动画重定向需要的源骨架路径和源 rest matrix。

`.anim` 导入部分支持：

- 解析位置、四元数旋转、欧拉旋转、缩放和浮点曲线。
- 按完整路径、路径后缀和唯一骨骼名匹配 Blender 骨骼。
- 按 AnimationClip 采样率生成 Blender Action。
- 将源骨架动画重定向到经过 Bone Orientation 调整后的 Blender 骨架。
- 可选忽略根运动、写入 Armature 对象或写入根骨骼。

## 2. 相关文件

- `__init__.py`：导入器注册、UI、参数定义和操作入口。
- `import_fbx.py`：FBX 层级、bind pose、骨架、蒙皮及轴转换处理。
- `unity_anim.py`：Unity 文本 AnimationClip 解析。
- `unity_anim_action.py`：曲线求值、坐标转换、骨骼匹配、动画重定向和 Action 写入。
- `test_assets/`：用于回归测试的 FBX 与 `.anim` 样例。

## 3. FBX 导入流程

核心流程可以概括为：

```text
解析 FBX
  -> 建立临时 Helper 层级
  -> 读取全局 bind pose / Cluster TransformLink
  -> 转换成逐骨骼局部 bind matrix
  -> 计算骨轴修正
  -> 创建 Armature、EditBone 和对象层级
  -> 绑定网格及权重
  -> 可选烘焙根对象轴转换
```

### 3.1 bind pose 本地化

FBX 的 Pose 和 Cluster 往往提供全局矩阵，而 Blender 骨骼层级需要局部 rest matrix。`make_bind_pose_local()` 递归执行：

```text
S_local = inverse(S_parent_global) * S_global
```

这里的 `S_local` 是后续动画重定向所称的“源 rest matrix”。如果 FBX 没有可用的 bind pose，则回退到节点自身变换。

骨骼矩阵使用 `normalized()` 去掉 bind matrix 中不适合直接写入 EditBone 的缩放，但保留平移和旋转。动画缩放仍由 AnimationClip 单独处理。

### 3.2 Bone Orientation 模式

#### FBX Original

保持 FBX 原始骨轴。最终 Blender 局部 rest matrix 与源 rest matrix基本一致，最适合验证动画坐标是否正确。

#### Blender Automatic

沿用 Blender FBX 导入器的自动朝向逻辑：根据子骨骼方向选择主轴，并将修正矩阵通过父子层级传递。

#### Better FBX Style

实现思路是保持每根骨骼的 head 不变，让 tail 指向有效子骨骼 head 的平均位置：

```text
target = average(child heads)
bone.tail = target
```

没有有效子骨骼时，优先沿父骨骼方向延伸；仍无法确定时使用当前骨轴生成一个最小长度，避免 Blender 折叠零长度骨骼。

改变 tail 会改变骨骼坐标轴，因此代码会计算源局部矩阵到目标局部矩阵的 orientation correction，并将其用于子骨骼构造。只改变 EditBone 外观而不保存这层关系，会导致动画旋转使用错误的局部轴。

### 3.3 为 AnimationClip 保存源信息

每根导入骨骼都会保存两个自定义属性：

- `unity_fbx_path`：FBX 原始骨骼层级路径，用于匹配 Unity 曲线中的 `path`。
- `unity_fbx_source_rest`：改变骨轴之前的局部 bind/rest matrix，共 16 个矩阵元素。

这些属性是 `.anim` 正确重定向的必要信息，不应因为关闭普通 Custom Properties 导入而省略。

## 4. Bake Axis Conversion

### 4.1 为什么 FBX 根对象会带 X 轴旋转

Unity/常见 FBX 使用 Y-up，Blender 使用 Z-up。Blender 官方导入器通常把坐标系转换保留在根对象变换上，因此 Armature 会显示约 `X=90°`，部分网格则可能保留一个相反的 `X=-90°` 局部旋转来抵消它。

视觉结果可能正确，但对象变换不干净，也容易给后续脚本和动画处理造成歧义。

### 4.2 烘焙方法

`bake_imported_root_axis_conversion()` 对每个无父级的导入根对象执行：

1. 保存根对象的 `matrix_basis`。
2. 将矩阵写入 Armature、Mesh、Curve 或 Lattice 数据。
3. 将根对象 `matrix_basis` 清为单位矩阵。
4. 修正普通子对象的 `matrix_parent_inverse`，保持场景外观不变。
5. 对仍带反向局部轴转换的直属网格，把其 `matrix_basis` 烘进几何数据，再清零对象变换。

网格变换会同时处理 shape keys。共享数据 `data.users > 1` 时不会贸然烘焙，以免同时改变其他实例。

骨骼挂接对象不会执行普通子对象补偿，因为它们会跟随已经烘焙的 rest pose。

空对象也不会执行“子网格变换清零”：Empty 没有几何数据可接收矩阵，直接清零会破坏武器、碰撞体和特效挂点的位置。

### 4.3 保存烘焙矩阵

当根对象是 Armature 时，原始根转换保存为：

```text
armature["unity_fbx_baked_axis_matrix"]
```

原因是 Bake 会从左侧改变根骨骼 rest matrix，而 `.anim` 曲线仍处于原始 FBX 骨架空间。动画导入器必须把这次“整体空间转换”和“逐骨骼朝向修正”区分开，不能把它们混成同一种 correction。

## 5. Unity `.anim` 导入

### 5.1 Unity 与 Blender 如何记录骨骼动画

Unity Transform 动画曲线通常记录的是每个节点相对父节点的局部 TRS：

- `m_PositionCurves`：`localPosition` 的绝对局部值，不是相对 bind pose 的位移增量。
- `m_RotationCurves`：`localRotation` 的绝对局部四元数，序列化分量顺序是 XYZW。
- `m_EulerCurves`：局部欧拉角。
- `m_ScaleCurves`：`localScale` 的绝对局部值。
- 每条曲线的 `path` 指向 Avatar/模型层级中的 Transform。

“绝对局部值”表示它已经包含节点的静态 local transform。例如某个子骨骼 rest position 是 `(0.1, 0, 0)`，动画曲线在静止帧附近也会记录约 `(0.1, 0, 0)`，而不是 `(0, 0, 0)`。

Blender 的 `PoseBone.location`、`rotation_quaternion` 和 `scale` 则是相对骨骼 rest pose 的姿态通道，组合后形成 `PoseBone.matrix_basis`。它们不是 Unity `localPosition/localRotation/localScale` 的直接对应物。概念上：

```text
Unity / FBX:
A(t) = 当前帧相对父节点的绝对局部矩阵

Blender PoseBone:
B(t) = 相对该骨骼 rest matrix 的动画增量
```

所以不能把 Unity 的位置和旋转数值直接写进 PoseBone 通道。至少要先计算：

```text
B(t) = inverse(rest) * A(t)
```

如果 Blender 骨轴经过 Better/Automatic 调整，还要继续进行第 6 节介绍的换基。

此外还有这些格式差异：

| 项目 | Unity | Blender |
| --- | --- | --- |
| 骨骼动画基准 | 父 Transform 局部空间 | 相对 EditBone/rest pose 的 pose basis |
| 四元数存储顺序 | XYZW | WXYZ |
| 常用坐标系 | 左手、Y-up | 右手、Z-up |
| 骨架静态姿态 | Transform 层级/local TRS | EditBone/Bone rest matrix |
| 曲线路径 | Transform 层级字符串 | RNA data path，例如 `pose.bones["Bone"].location` |
| 曲线组织 | Clip 内按绑定路径分类 | Action/Slot/ChannelBag 内的 F-Curve |

Blender Bone 的 `matrix_local` 是骨骼在 Armature 空间中的 rest matrix；要取得类似 Unity “相对父节点”的局部 rest matrix，还需要：

```text
rest_local = inverse(parent.matrix_local) * bone.matrix_local
```

Armature 对象本身的 location/rotation/scale 则是普通对象变换，不属于 PoseBone rest offset。这也是 Root Motion 写到 Armature 与写到 Root Bone 必须采用不同处理方式的原因。

### 5.2 文本解析

`unity_anim.py` 读取 Unity YAML 风格文本中的：

- `m_RotationCurves`
- `m_EulerCurves`
- `m_PositionCurves`
- `m_ScaleCurves`
- `m_FloatCurves`
- `m_EditorCurves` 中的 `m_LocalRotation.*`、`m_LocalPosition.*` 和 `m_LocalScale.*` 分量曲线
- Clip 名称、采样率和开始/结束时间

关键帧使用 Unity 保存的 value、inSlope 和 outSlope。导入时按采样率逐帧求值，并使用三次 Hermite 插值：

```text
p(t) = h00*v0 + h10*dt*m0 + h01*v1 + h11*dt*m1
```

非有限斜率会回退到两关键帧之间的割线斜率，避免产生 NaN 或无限值。

Unity YAML 会把较长的未加引号 `path` 折到下一行。例如 `.../Bip001 L` 后一行可能继续为 `Clavicle/Bip001 L UpperArm`。解析器必须按 YAML plain scalar 规则以空格拼回续行；若只读第一行，大量左/右侧骨骼会坍缩成同一个路径，最终重复创建同一根骨骼的 F-Curve。

`m_EditorCurves` 是逐分量曲线。解析器按完整 path 与属性前缀重新组合 XYZW 四元数或 XYZ 向量，并以它覆盖同路径的 native 曲线；若某组分量不完整，则保留 native 曲线。

### 5.3 骨骼路径匹配

匹配顺序为：

1. `unity_fbx_path` 精确匹配。
2. `path_<uint32>` 与骨骼路径的 Unity CRC32 哈希匹配。
3. Blender 骨骼完整路径匹配。
4. 唯一路径后缀匹配。
5. 对不含 `/` 的单节点绑定，允许唯一叶节点骨骼名匹配。

多层级路径匹配失败后不能退化为叶节点名。例如 `.../Neck/Bip001` 并不是根骨骼 `Bip001`；强行按叶名绑定会使多个源节点同时写入根骨骼并产生重复 F-Curve。

不能匹配的路径写入 Action 的 `unity_unresolved_paths`，而不是静默绑定到可能错误的同名骨骼。

样例中的 5 个 unresolved 路径属于武器 Empty 和脸部网格，不是 Armature 骨骼，因此不应强行映射为 pose bone。

### 5.4 Unity 局部空间到 FBX 骨架空间

最关键的约定是：骨骼 AnimationClip 不能直接转换到 Blender 世界坐标。

导入后的 Armature 对象，或者 Bake 后的 Armature 数据，已经承担了 Y-up 到 Z-up 的整体转换。骨骼局部曲线只需要从 Unity 左手系映射回当前 FBX 骨架使用的局部空间。

本模型对应的局部平移转换为：

```text
(x, y, z)_Unity -> (-x, y, z)_FBX
```

这是 X 反射。对于四元数的虚部，反射共轭对应：

```text
(w, x, y, z)_Unity -> (w, x, -y, -z)_FBX
```

四元数曲线在相邻采样之间还会检查点积。如果点积小于 0，则把后一个四元数整体取反，以避免 `q` 和 `-q` 表示同一旋转却在 FCurve 中产生不必要的长路径跳变。

欧拉曲线先按 Unity 使用习惯以 `ZXY` 顺序转换为四元数，再执行相同的手性转换。

对象级 Root Motion 是另一种空间。实现中先在源 FBX 骨架空间计算相对首帧的根运动，再用已保存的轴转换矩阵进行共轭换基，不能把骨骼局部数值直接写到 Armature 对象。

### 5.5 RootT/RootQ 与普通根骨骼曲线

Unity 文本 Clip 中可能同时存在：

- 根骨骼路径（例如 `Bip001`）自己的 Position/Rotation 曲线。
- `m_FloatCurves` 中的 `RootT`/`RootQ` 或 `MotionT`/`MotionQ`。

在当前测试 Clip 中，`RootT/RootQ` 与 `Bip001` 的普通局部 Transform 曲线是同一根变换的两种记录，不是两段应该相乘的独立运动。若同时写入根骨骼或同时作用于人物，会造成重复位移/旋转。

三种选项的当前语义为：

- `Ignore`：忽略额外的 RootT/RootQ 数据；普通根骨骼 Transform 曲线仍按 Clip 内容导入。
- `Root Bone`：用 RootT/RootQ 写入根骨骼 pose 通道。若普通曲线已经创建了同名 F-Curve，则清空并复用，而不是重复创建。
- `Armature Object`：提取 RootT/RootQ 相对首帧的运动到 Armature 对象，并从根骨骼局部动画中消去相同增量。因此人物最终世界姿态不变，但位移/朝向运动位于对象层，根骨骼只保留原地姿态。

设 RootT/RootQ 合成的源局部矩阵为 `R(t)`，首帧为 `R0`，提取的根运动为：

```text
D(t) = R(t) * inverse(R0)
```

写入 Armature 时，根骨骼源动画同步变为：

```text
A_in_place(t) = inverse(D(t)) * A(t)
```

如果启用了 Bake Axis Conversion，保存的轴矩阵为 `G`，Armature 对象中的根运动要换到 Blender/baked 空间：

```text
D_blender(t) = G * D(t) * inverse(G)
```

未 Bake 时，Armature 原有对象矩阵必须保留，并与 `D(t)` 组合；不能从单位矩阵开始写，否则会丢失原本让角色站立的坐标转换。

### 5.6 Humanoid muscle 曲线

Unity 的 Generic/Legacy Clip 通常保存逐 Transform 的局部 TRS；Humanoid Clip 则可能只为少数辅助节点保存 Transform 曲线，人体主体动作保存为 `m_FloatCurves` 中的 muscle，例如 `Spine Front-Back`、`Left Upper Leg Front-Back`。因此仅解析 Transform 曲线时，会出现“只有一根手指末端或少数挂点在动”的现象。

muscle 值不是某根骨骼可直接使用的欧拉角或四元数。Unity 会结合 Avatar 的人形骨映射、零姿态、pre/post rotation、关节活动范围及 twist 分配，将 muscle 值求解成每根骨骼的局部旋转。不同导出来源可能保存标准化值，也可能保存角度值；仅有 `.anim` 文件时不包含完整 Avatar 定义，无法对任意骨架做精确通用求解。

当前插件采用独立的 `humanoid_biped_profile.py`：

- 内置常见 `Bip001` 骨架的零 muscle 局部四元数与每个 muscle 在 `-1/+1` 端点的标定结果。
- profile 生成阶段已经把 Unity 序列化的 `XYZW` 转成 Blender/mathutils 的 `WXYZ`，运行时不能再次重排。profile 中脊柱的 `(1, 0, 0, 0)` 是单位旋转；小腿的端点数据也以 WXYZ 表示关节屈伸。
- 根据当前值选取正/负端点，把相对零姿态的旋转转为 axis-angle 后按幅值缩放，并按标定的乘法顺序合成。若曲线表现为角度制，则利用 profile 端点推导每个 muscle 的正负角度范围，再换算成标准化幅值。
- GI 示例的若干密集 muscle 曲线含有明显的单帧尖峰，例如同一手指曲线连续出现约 `0.09 → 84.40 → 10.46 → 0.09`。Humanoid 专用求值会先做局部中值去尖峰和平滑；普通 G 模式 Transform 曲线不经过此处理。
- profile 的绝对局部旋转属于标定 Avatar，不能直接作为目标 FBX 的绝对局部旋转。实现使用 `A_target = A_profile * inverse(S_profile) * S_target` 完成局部 rest-to-rest 换基。这样零 muscle 必然得到目标骨骼的 rest pose，也不会把 Unity Avatar 的绝对 pre/post rotation 强加到 GI FBX。
- 合成结果仍进入第 6 节的 source-rest/Bone Orientation 换基流程，因此 Original、Better 与 Bake 模式共用同一套重定向。
- 同一骨骼同时存在原生 Transform 曲线和 muscle 曲线时，以 Transform 曲线为准；这样不会覆盖动画作者显式制作的手指、武器或飘带动画。
- Unity 序列化使用的 `LeftHand.Index.1 Stretched` 等名字会转换为 profile 使用的 HumanTrait 名称。

这是一个面向该资产族的 `Bip001` 标定 profile，不是任意 Unity Avatar 的通用求解器。其它骨架若骨名、零姿态或 Avatar 限位不同，需要新增独立 profile。插件运行时只读取 FBX、文本 `.anim` 与本地 Python 数据，**不会启动、调用或依赖 Unity Editor/Unity 工程**。

## 6. Bone Orientation 后的动画重定向

### 6.1 符号约定

对某根骨骼定义：

- `S`：保存的源局部 rest matrix。
- `T`：最终 Blender 骨骼相对父骨骼的局部 rest matrix。
- `C_p`：父骨骼的朝向修正。
- `C`：当前骨骼的朝向修正。
- `A(t)`：Unity 动画在源骨架空间中的局部矩阵。

目标 rest matrix 满足：

```text
T = inverse(C_p) * S * C
```

因此当前骨骼修正矩阵为：

```text
C = inverse(S) * C_p * T
```

代码按骨骼层级从父到子计算所有 `C`。

### 6.2 写入 PoseBone 的 matrix_basis

动画局部矩阵重定向到目标骨轴后为：

```text
A_target(t) = inverse(C_p) * A(t) * C
```

Blender PoseBone 通道需要的是相对于目标 rest pose 的 `matrix_basis`。代入上面的 rest 关系后可化简为：

```text
B(t) = inverse(C) * inverse(S) * A(t) * C
```

这就是 `unity_anim_action.py` 实际使用的核心公式。

其意义是：

1. `inverse(S) * A(t)` 得到源骨架相对 rest pose 的动画增量。
2. 用 `C` 对动画增量做共轭变换，把增量表达在新骨轴中。
3. 最后将矩阵分解为 location、quaternion 和 scale，写入 Blender FCurves。

当 Bone Orientation 为 Original 时，`C` 接近单位矩阵，公式自然退化为普通的 `inverse(S) * A(t)`。

### 6.3 Bake 对根骨骼的特殊影响

Bake 使用整体矩阵 `G` 左乘根骨骼：

```text
S_root_baked = G * S_root
```

`G` 是坐标空间变换，不是骨骼自身的朝向修正。如果直接用 baked rest matrix 推导 `C`，就会把 `G` 错当成逐骨骼 correction，产生大幅 pose location，并使 Better 模式重新躺倒或扭曲。

因此 `_bone_retarget_corrections()` 读取 `unity_fbx_baked_axis_matrix`，只在推导根骨骼 rest 关系时恢复这层左乘关系。最终动画增量中的 `G` 会在 rest inverse 与动画矩阵之间相消，不应重复写入每根局部通道。

## 7. 曾出现的问题及根因

### 7.1 网格随动画反复拉伸、腿向大腿反折

旧实现把每根 Unity 局部骨骼曲线都执行了 Y-up 到 Z-up 转换，同时又让 Armature 根对象承担相同转换，等于在骨骼层级中重复转换坐标轴。

位置曲线因此不再与 bind pose 一致。层级越深，错误局部位移累积越明显，所以清除所有 PoseBone location 会暂时消除大部分拉伸，但根骨骼方向和需要真实位移的骨骼仍然错误。

修复方式是：骨骼局部曲线转换到 FBX 骨架局部空间，整体坐标转换只由 Armature 对象或 baked Armature 数据承担。

### 7.2 清除位移后人物倒置，脸和脚趾仍扭曲

清除 location 只掩盖了位置空间不一致，没有修复旋转轴不一致。脸部、脚趾等末端骨骼通常没有可靠子节点来定义骨轴，也更容易暴露错误的四元数空间转换。

正确做法是保留源 rest matrix，并将完整的局部动画增量通过 correction 共轭到目标骨轴，而不是单独删除某一类通道。

### 7.3 Better 模式下动画整体躺倒

Better 模式改变了 EditBone 的局部坐标轴，但旧动画仍按源骨轴解释。直接把 Unity 四元数写入新骨轴，相当于改变了旋转的参考系。

修复方式是使用 `inverse(C) * delta * C`，将源骨轴下的动画增量转换到目标骨轴。

### 7.4 Bake 后 Body 仍显示 X=-90°

根 Armature 的轴转换被烘焙后，部分直属 Mesh 仍保留原来用于抵消根旋转的局部 `X=-90°`。虽然通过父级补偿后外观可能正确，但对象属性并不干净。

修复方式是把该局部矩阵继续烘入单用户网格数据，并清零 Mesh 对象变换。必须限制为具有可变换数据的对象；不能对 Empty 使用同样逻辑。

### 7.5 矩阵乘法顺序

本插件采用 Blender/mathutils 的列向量语义。矩阵乘法不可交换：

```text
parent @ local
```

与：

```text
local @ parent
```

含义完全不同。尤其要区分：

- 整体坐标空间转换通常从左侧乘入。
- 骨骼局部轴修正通常出现在矩阵右侧。
- 动画增量换基需要共轭 `inverse(C) @ delta @ C`。

调试时如果只看欧拉角，很难发现乘法方向错误；应直接比较矩阵、骨骼局部位移和最终蒙皮结果。

## 8. Action 写入细节

插件使用 Blender 5.x Action Slot/ChannelBag API：

1. 新建 Action。
2. 为 Armature 创建 Action Slot。
3. 获取 ChannelBag。
4. 为每根映射骨骼创建 location、rotation_quaternion、scale FCurves。
5. 批量写入采样后的关键帧坐标。

每帧写入 10 个通道：

```text
location XYZ
rotation_quaternion WXYZ
scale XYZ
```

Action 还记录：

- `unity_source_sample_rate`
- `unity_unresolved_paths`
- `unity_root_motion`
- `unity_humanoid_profile`
- `unity_humanoid_bones`

## 9. 验证与回归测试

建议每次修改坐标或骨架代码后至少验证以下组合：

| Bone Orientation | Bake Axis Conversion |
| --- | --- |
| FBX Original | 关闭 |
| FBX Original | 开启 |
| Better FBX Style | 关闭 |
| Better FBX Style | 开启 |

检查项：

1. 静态模型站立方向正确，Armature、LOD 和主体网格重合。
2. Bake 开启后 Armature 和 Body 等根层级对象旋转为零。
3. 导入 `.anim` 后躯干、大小腿、脚、头部没有异常 PoseBone location。
4. Original 与 Better 的最终蒙皮姿态应一致，仅骨骼显示轴不同。
5. 动画过程中网格不拉伸、不反折，脸部和脚趾没有异常扭曲。
6. 武器、碰撞体、特效挂点等 Empty 不因 Bake 丢失局部变换。
7. Quaternion 相邻关键帧没有符号翻转造成的插值跳跃。

本次使用 `test_assets/Avatar_Female_Size02_Anbi.fbx` 与对应 Hit Shake AnimationClip 验证：

- Original 和 Better 模式均成功导入动画。
- Bake 开关不再改变最终 pose。
- 主要身体骨骼的非预期局部位移接近零。
- 两种骨轴模式的同帧蒙皮渲染一致。
- Body 对象旋转归零。
- 135 条骨骼路径成功映射；5 条未解析路径属于非骨骼对象。

使用 `test_assets/gi/Avatar_Lady_Sword_Clorinde.fbx` 与 `Ani_Avatar_Lady_WalkStopL.anim` 验证：

- 11 条原生 Transform 骨骼路径和 45 根 Humanoid profile 骨骼成功生成动画，共 56 根。
- 脊柱、大小腿、手臂、手和非显式 Transform 手指均具有 F-Curve，不再只有末端手指运动。
- Original/Better 与 Bake 开/关四种组合均可导入，抽查帧的蒙皮包围盒稳定，无异常拉伸爆炸。
- Root Motion 的 Armature 与 Root Bone 两种模式均可导入且不再发生重复 F-Curve 错误。
- 6 条未解析路径对应当前 FBX 中不存在的武器、披风/飘带辅助节点；这符合“通用人物动画与飘带动画分离”的资产组织方式，不应误绑到人体骨骼。

使用重新验证的 `Ani_Avatar_Lady_WalkStopL_Ori.anim` 验证：

- 该文件是 G/Transform 动画，包含 76 组旋转、位移和缩放曲线，不包含 Humanoid muscle。
- YAML 路径续行恢复后得到 76 个唯一完整路径；其中 68 个映射到 Clorinde 骨架，8 个属于武器、披风、相机、Root/Motion 等当前骨架外节点。
- 不再把 `.../Neck/Bip001`、`.../Foot/Bip001` 错绑到根骨骼，也不再重复创建 `pose.bones["Bip001"].location`。
- Original/Better 与 Bake 开/关四种组合均成功导入，抽查帧矩阵均为有限值，Original 与 Better 的 Body 包围盒一致。

## 10. 后续维护注意事项

- 不要删除 `unity_fbx_source_rest`，除非同时提供等价的源 rest 数据来源。
- 不要把对象级 Unity→Blender 转换函数用于 PoseBone 局部曲线。
- 新增骨骼朝向算法时，只要最终 rest axis 改变，就必须让动画重定向能够推导对应 correction。
- 修改 Bake 时要分别验证 Armature 数据、Mesh 数据、普通子对象、骨骼挂接对象和 Empty。
- 非均匀缩放经过任意骨轴换基后理论上可能产生 shear；Blender 的 location/quaternion/scale 通道不能完整表示任意 shear。当前样例中的缩放轴与骨轴修正兼容，但更复杂资产应额外测试。
- Root Motion 写入 Armature 对象与写入根骨骼使用的目标空间不同。扩展该功能时应分别推导，不能简单复用普通骨骼曲线路径。
- Unity 的 RootT/RootQ 是否与根 Transform 曲线重复、是否已按 Import Settings 烘焙，会随资产导出方式变化。遇到其他来源的 Clip 时，应先比较两组曲线再决定提取策略。
- 当前 Humanoid 支持是 `Bip001` 标定 profile；若要支持任意 Avatar，必须另外取得该 Avatar 的映射、零姿态、关节范围和 twist 参数，不能从 muscle 曲线名称凭空恢复。
- 压缩/二进制 AnimationClip 仍不在当前文本解析器的支持范围内。
