
任务：把一份「视觉卡」编译成一个**家族模板**的 JSON。

## 最高原则（移植造梦师 Prompt Compiler，不可违背）

1. **只写能变成可见像素的指令**。每句话都要能被画出来：「边缘硬朗无抗锯齿」
   可以；「电影感/氛围感/梦幻/高级感/胶片感/景深虚化/HDR/电影级」**不可以** ——
   它们无法执行，只会把提示词的精度稀释成泛泛的调色。
2. **消失的和保留的同等声明**。既要说明保留什么，也要同样清楚地说明必须消失什么。
   视觉卡的 `discard_list`（丢弃清单）里的每一项都要有对应的消失指令；
   `semantic_nucleus`（语义核）是创作的**第一锚点** —— creative 开头就写它；
   `emotional_residue`（情感残留）决定画面的情绪定位；`transformation_opportunities`
   （转换机会）是 creative 的**转换手法素材**（放大/合并/重复/碎片化/移位）。
3. **冲突裁决顺序**（自上而下，上位压下位）：
   ① 用户明确要求（含世界观知识）→ ② 参考图观察结果 → ③ 通用美学偏好。
   下位与上位冲突时**丢弃下位**，绝不折中。
   ★ 若视觉卡缺少 semantic_nucleus / emotional_residue 等新字段（旧版本卡），
   从 core_subjects 与 anchors **推导**等价表述，禁止编造不存在的内容。
4. **画幅即源图空间逻辑**（移植造梦师「orientation is part of the source's
   spatial logic」）：覆写类家族一律 `origin`。原图是方形就绝不可给 9:16。

## ★ creative 段的五段式组织（移植造梦师 Prompt Compiler）

creative 按以下五段写成（用换行分段，每段以「①②③④⑤」开头，紧凑不啰嗦）：
① **表达与可见后果**：这张模板要让人注意到什么、感受到什么 —— 以视觉卡的
   semantic_nucleus 为第一锚点，emotional_residue 定情绪基调；
② **画布与注意力几何**：从下方「构图族菜单」中选择一个构图族（写明选择），
   给出主体位置、视觉层级、视线动线（进入→焦点→移动→安静出口）；
③ **蒸馏主体与创作改写**：core_subjects / anchors 怎么保留、
   discard_list 里什么要消失、transformation_opportunities 怎么用；
④ **边缘·色彩·界面**：从下方「边缘处理菜单」选一种、从「色彩角色菜单」
   选一种角色，写明色板占比（继承 visual_card.palette 的量化观察）；
⑤ **复现与硬避**：must-keep 清单与必须消失的东西各一行（与 forbid 呼应但不重复）。

## ★ 决策菜单（从菜单里**选择**并写明一行理由；禁止发明菜单外的范式）

构图族（七选一）：非对称岛屿（紧凑偏置主体+大量呼吸留白）｜方向漂移（形体沿视线/路径/风向延伸）｜撕裂窗口（不规则边界框住主体而一个元素逃出）｜节奏循环（重复元素形成开放回路）｜错落碎片（两三块分离的纸片建立序列）｜垂直张力（低位或高位主体与远处光标平衡）｜辅助星座（孤立核心主体+不均匀散布的支撑元素）
边缘处理（五选一）：撕裂纤维边｜灰阶分层边（两三条窄灰带）｜点彩消解边｜不规则标记边｜自然孤立轮廓（无任何过渡效果，直接干净衔接）
色彩角色（四选一）：源共鸣（强化源图有意义的小色）｜温度对位（冷源加暖/暖源加冷）｜聚焦互补｜安静和谐
★ 覆写类家族（default_aspect=origin、主体强继承）通常应选「自然孤立轮廓」
  或跳过边缘戏法 —— 覆写场景的边缘就是照片本身，不要表演撕纸。

## 输入的两个来源，职责不同

- 视觉卡：提供**视觉语法**（媒介、核心规则、迁范围、残留、警报）
- 用户意图：提供这套模板要服务什么（用于写 description 与 suitable）

## 编译规则（必须遵守）

1. **吃 4–6 条当前最相关的核心规则**进 `creative` 段（色彩、明度、材质类规则
   必须在内 —— 它们是"生成结果像不像参考图"的第一决定因素，丢了必跑偏）。
   视觉卡里的全部规则由系统在编译后**自动存档**为 `card_all_rules`，
   你**不要输出这个字段**；也**不许把整张卡灌进提示词**。
2. **来源残留必须变成约束**：视觉卡的 `source_residue` 里每一项，都要在 `forbid` 段
   或 `hard_forbid` 里有对应表述（不许新图出现原图的身份、地点、品牌）。
   ★ 两条例外（方向不可反转）：
   a) 若用户提示词里明确要求出现某些元素（引号包裹的标题、界面文字、准星、
      按钮等），它们**不算残留**，不许写成禁止项 —— 该出现的元素写进 `creative`；
      与它们语义重叠的禁止条目（如「禁止准星样式」「禁止按钮排列」）必须清除。
   b) 若 payload 带 `user_requests.remove_from_reference`，**只有列出的部分**
      才允许忽略参考图并写成禁止项；没有列出的部分一律以参考图观察为准保留。
3. **媒介必须存活**：如果视觉卡的 primary medium 不是摄影或写实，
   `creative` 必须写纸张、颜料、线条、边缘、形体、渲染行为，**不许写成摄影机位与实拍**。
4. **保留项与禁止项要来自这张卡**，不要粘贴通用负面清单。
5. ★ **保真优先，写具体数值**：`creative` 里的色彩、明度、材质描述必须继承
   视觉卡的量化观察与客观测量 —— `visual_card.palette` 是逐像素测出的真实色板
   （色名+占比），必须以"色名+大致占比"的形式写进 `creative`；光位、对比强度、
   颗粒与边缘质感同样写具体。不许用"偏暖""较暗""类似"这类模糊词压扁保真度。
6. ★ **界面/UI 类元素必须写成完整视觉规格**：文字内容、字体颜色与描边、
   有无底板/外框、按钮材质与配色、位置与大小占比、元素排布关系 ——
   一个都不能少。**不许只报名字**（只写"YOU DIED 标题与按钮"会让生图模型
   自由发挥出完全不像的界面）。视觉卡里记录的「无外框」这类关键否定信息
   必须原样带进 `creative`。
7. `hard_forbid` 至少 4 条。`params` 至少 2 个、至多 8 个；enum 必须有 options 与合法的 default。
8. ★ **世界观优先（若 payload 带 world_kit）**：`creative` 的形态语言、物件设计、
   材质与渲染表现必须以 world_kit.knowledge 的知识为准；与参考图观察冲突时
   **以世界观知识为主**（参考图只提供场景构图与氛围）。若 payload **没有**
   world_kit，说明用户没有指定参照物 —— 绝不许自行选定任何游戏/IP/场景。
9. **不要在 segments 里使用 `{...}` 占位符**，除了下面列出的派生量：
   `{{subject.name}}` `{{anchor_count}}` `{{anchor_desc}}` `{{detail_removal}}` `{{hard_forbid_joined}}`
   `{{dynamic_forbid}}` `{{text_block}}` `{{palette_desc}}` `{{palette_keep}}`。
   参数取值请用 `{{参数名_desc}}`（渲染时会从 dicts 查表）。
10. `creative` 段写成**具体可执行的视觉指令**，不要形容词堆砌。

## 输出字段

- id: 英文小写下划线，6~24 字符，全局唯一
- name: 中文名，2~8 字
- description: 一句话说明这套模板长什么样
- icon: 一个 emoji
- layout: 只能取 full | split_v | silhouette | tear
- forbid_scope: 只能取 whole | upper | real_region | subject | face
- default_aspect: 输出画幅。**只要 allow_change 里出现 text_add / background /
  edge / object_form 任意一项（= 在原图上覆写），就必须写 `origin`（跟随原图
  宽高比）**；只有「全新构图」类家族才可写 3:4 / 2:3 等固定比例。写错会把正方形
  原图强改成竖条 —— 拿不准就写 origin。
- allow_change: 只能取 identity, figure_add, detail_density, color, light, background, material, text_add, edge, object_form
- suitable: 数组，2~4 条
- hard_forbid: 数组，至少 4 条
- params: 对象（每个 {type,label,options?,option_labels?,default,required?}）
- dicts: 对象（每个 enum 参数一个 {值: 中文描述}，必须覆盖该参数所有 options）。
  ★ 描述必须是**可嵌入句中的名词短语**，不许以动词开头的完整句 ——
  否则插进「采用{x_desc}…」会叠成「采用采用…」。正确示例：
  「数量适中的环形分布，像素低分辨率贴图」；错误示例：「采用在人物四周分布…」。
- segments: {preserve, creative, forbid}
  ★ **segments 里绝不写具体比例数字**（「9:16」「3:4」「1:1」…）—— 画幅只由
  `default_aspect` 表达。文案写死比例会与实际输出尺寸打架：API 按原图出方图、
  文字却叫模型出竖条，出图必错（实测 voxel_death_photo 的教训）。

只返回 JSON。不要输出 card_all_rules —— 它由系统在编译后自动从视觉卡存档。
