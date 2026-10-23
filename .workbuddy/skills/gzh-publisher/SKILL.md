---
name: gzh-publisher
description: 把一篇成稿写进公众号后台：存草稿（标题/摘要/正文）、自动设封面、点到发表二维码、按 appmsgid 清草稿。基于 agent-browser 浏览器自动化（个人订阅号没有官方接口权限）。当用户说「编进草稿箱」「存草稿」「发布这篇」「设封面」「清理草稿」「发到公众号」时使用。**发表必须扫码，本技能只能把人送到二维码前**，不承诺无人值守发表。
agent_created: true
---

# 公众号发布（草稿 / 封面 / 发布）

## 用途

一条命令把 HTML 成稿变成后台草稿，自动配好封面，并可推进到「微信验证二维码」——最后一下由人扫。

**它不是"发稿机器人"，是"把人送到最后一步"的工具。** 这个界限是平台定的，不是保守设定，原因见下方「硬边界」。

## 定位工作区

**脚本自带在本技能的 `scripts/` 下**（不再依赖工作区里那一份）。工作区根按这个顺序确定：

1. 环境变量 `GZH_WORKROOT`
2. 当前目录往上，含 `排版规范.md` / `组件库.md` / `来源清单.md` 任一个的目录
3. 当前目录
4. 脚本上两级

所以**换电脑、换目录名都不用改代码**。文档随技能走：

| 位置 | 内容 |
|---|---|
| `references/组件库.md` | 13 个组件片段（唯一事实源，照着抄） |
| `references/排版规范.md` | 四条铁律 + 组件索引 + 5 种拼法 + 自检 |
| `references/写作规则.md` | 两条轨道、交付物格式 |
| `references/实测约束.md` | 所有踩过的坑 |
| `assets/模板/template.body.html` | 样板 html |

工作区根如果也有同名文件，**以工作区根为准**（方便你在项目里临时改一份）。

| 脚本 | 作用 |
|---|---|
| `scripts/gzh_doctor.py` | **换电脑先跑这个**：环境自检 |
| `scripts/gzh_queue.py` | 稿件队列：`list / next / mark / prune / sync`，决定这次发哪一篇 |
| `scripts/gzh_state.py` | 状态与台账（draft.json + ledger），队列与闸门都依赖它 |
| `scripts/gzh_prep.py` | 一条命令备稿：选稿 → 抓原文 → 封面 → 建目录 → 写简报 |
| `scripts/gzh_covercard.py` | 源上没图时渲染中文封面卡（900×383） |
| `scripts/gzh_lint.py` | 内容 lint（字数/加粗/导语/来源行） |
| `scripts/gzh_draft.py` | 存草稿：标题 / 摘要 / 正文 / 封面 |
| `scripts/gzh_publish.py` | 发表：闸门 → 弹窗 → 二维码（三级模式） |
| `scripts/gzh_cleanup.py` | 按 appmsgid 删草稿（默认只列不删） |
| `scripts/gzh_source.py` | 从 URL 备素材：抓正文 + 抽封面（优先 og:image）+ 生成 manifest 骨架 |
| `scripts/gzh_cover.py` | 从源文抽一张横图当封面素材（og:image 优先，其次正文图） |

## 换电脑/新环境：三步

```bash
# 1 自检（不联网，1 秒）—— 缺什么会直接告诉你，并给出修复命令
python scripts/gzh_doctor.py

# 2 登录一次（会开浏览器，扫码）
python scripts/gzh_doctor.py --login

# 3 验证能写稿
python scripts/gzh_draft.py --manifest 稿件.json --check-only
```

登录态存在 `<工作区根>/.cache/browser-profile`，**换电脑必须重新扫一次码**（cookie 不能拷贝，换了机器/账号会失效）。
也可以设 `GZH_WORKROOT` 指向你的工作区，脚本就能在任何目录下运行。

## 前置条件（缺一样都跑不动）

1. **Python ≥ 3.10**（脚本用了 `X | None` 语法）
2. `agent-browser` + Chrome 已装（`npm i -g agent-browser && agent-browser install`）
3. 登录态已存在 `.cache/browser-profile`：
   ```
   agent-browser --args "--no-sandbox,--do-not-de-elevate" \
     --profile "<工作区根>/.cache/browser-profile" --headed open https://mp.weixin.qq.com/
   ```
   扫一次码即可，之后脚本复用。**注意：扫码必须是该公众号的管理员/运营者微信号**，
   否则会停在「请联系管理员验证」，发表永远不完成。

## 每天怎么发（库存制）

**每篇发表都要扫一次码**，这是平台硬限制（群发开关、定时发表都试过）。所以策略是：
每天写 2–3 篇存草稿，**只发 1 篇**（扫 1 次），剩下的排队到后几天。

```bash
python scripts/gzh_queue.py list --json          # 库存 / 待补 / 今天发过没有
python scripts/gzh_queue.py next --json          # 下一篇该发哪个（FIFO，24h 内扫码超时的优先）
python scripts/gzh_publish.py --from-queue --mode qr-wait --wait-scan 240 --headed
```

- 队列排序：**FIFO 优先**，不是星级优先（星级优先会让高分新稿插队、旧稿烂到过期）
- 超过 4 天的稿 `prune` 标 stale，不会再发
- 一天只能发 1 篇：闸门读台账，发过就 `exit 4`
- `--headed` 必须开，否则人扫不到码

## 怎么用

**存草稿**（最常用）：
```bash
python scripts/gzh_draft.py --manifest article.json
python scripts/gzh_draft.py --manifest article.json --appmsgid 100000042   # 续编辑
```

manifest：
```json
{
  "title": "标题（≤64 字）",
  "summary": "摘要（≤120 字）",
  "body_html_path": "body.html",
  "cover_path": "cover.jpg",
  "collection": "模型前线"
}
```

`collection`（选填）= 公众号的**分类栏**，即「合集」。**每篇最多 1 个**（编辑器限制）。
也可以在命令行给：`--collection 模型前线`。
建议的分类见工作区根 `分类方案.md`。

**发布**（默认只跑闸门，不会误发）：
```bash
python scripts/gzh_publish.py --appmsgid 100000042 --mode dry-run   # 只看闸门
python scripts/gzh_publish.py --appmsgid 100000042 --mode dialog    # 点到弹窗
python scripts/gzh_publish.py --appmsgid 100000042 --mode qr        # 一路点到二维码，截图存证
```

**清草稿**：
```bash
python scripts/gzh_cleanup.py                                  # 只列
python scripts/gzh_cleanup.py --drafts 100000042 --yes         # 真删
```

## 硬边界（不要让用户误以为能无人值守）

- **发表必须人扫一次码。** 2026-10-19 实测：无论群发通知开还是关，「继续发表」之后都会弹「微信验证 / 扫码后，请联系管理员进行验证」。**关群发通知躲不过去**——这一点已经试过了，别再重复试。
- **登录态会被踢，不是过期。** cookie 有效期 4 天，但会因**单点互踢**（用户在自己浏览器登录后台）或高频操作而提前失效。遇到就报错停下、提示重扫，**绝不要挂着重试**。
- **原文链接字段填不进去**：`source_url_checked` 开关无法程序化勾选。署名写进正文（`信息来源：XXX`）代替。
- **素材库的图删不掉**：素材页悬停只有重命名输入框，无删除入口；「管理」是管分组。只能不管它。

## 关键约束（改代码前必读）

这些是踩了很多次才确定的，违反任何一条都会"看起来成功、实际没生效"：

1. 每个 `agent-browser` 调用必须是**一个自包含 batch**（`open + wait + 全部动作`）。守护进程活不过一次进程调用，跨调用依赖页面状态一定失败。
2. 必须传 `--args "--no-sandbox,--do-not-de-elevate"`（当前 shell 是管理员权限，Chrome 会自我去提权后退出）。
3. **不能**用管道捕获 agent-browser 输出（`capture_output=True` / `| head` 会永久卡死），必须重定向到临时文件。
4. **视口必须 `set viewport 1440 1200`**。默认 1080×498 会把弹窗底部按钮切到视口外，点击坐标全部打空。
5. **封面是否设置成功，只看 `.js_cover_preview_new` 的 `background-image` 是否指向 `mmbiz.qpic.cn`**。「拖拽或选择封面」这段文案始终留在 DOM 里（隐藏），永远为真，不能用来判断。
6. **摘要必须写在正文之后**（微信摘要默认抓正文开头，先写会被覆盖）。
7. 标题走 `.ProseMirror`[0]，正文走 `.ProseMirror`[1]；**不要**碰隐藏的 `textarea#title`。
8. `offsetParent` 对 fixed 弹层是 null，判断弹窗内元素可见性要用 `getBoundingClientRect()`。
9. **正文段落必须用 `<section>`，不能用 `<p>`**：`<p>` 会被编辑器合并压平成一块，
   加粗和内联样式全丢。样式写在 `<span>` 上，`<strong>` 与 `<span>` 平级。
   整篇一次性 `insertHTML`；**不要**拆成多次（会嵌套变形），也**不要**用 JS 的 `.click()` 聚焦正文区
   （不移动焦点，等于打在空气上）——要用 agent-browser 的真实 `click`。保存前脚本会核对块数与加粗数，对不上就拒存。

详细推导与踩坑记录见 `references/实测约束.md`。

## 正文版式

正文不是「能显示就行」——手机上排版崩掉，内容再好也没人读完。

- 规范：`references/排版规范.md`（四条硬纪律 + 组件索引 + 5 种推荐拼法）
- **组件库：`references/组件库.md`**（13 个组件的 HTML 片段**唯一事实源**，只从这里复制，不要自己写样式）
- 样板：`assets/模板/template.body.html`（「分析型」示例，可参考但不再是唯一骨架）
- 四条硬纪律：段落只用 `<section>`、样式只写 `<span>`、**每段 ≤90 字**、**一个强调色 `#1a4d8f`**

微信只保留内联样式，`<style>`、class、`<ul>`、`<h2>` 都会被剥掉或打回普通块，
所以样式必须写在每个标签的 `style` 里，并列内容用 C05 要点条目而不是 `<ul>`。
多列卡片（C09/C11/C12）依赖 `span display:inline-block`，必须配 `font-size:0` 父级 + 零空白 + 宽度 ≤98%。

封面：头条须 **2.35:1（900×383）**，出稿前用
`~/.workbuddy/skills/wechat-mp-typeset/scripts/cover_check.py 封面.png` 校验，按提示裁剪。

## 交付前自查

- [ ] `--dry-run` 七项闸门全绿（标题 / 正文≥300 / 封面 / 发表按钮 / 急停 / 当天未发 / 原创核验）
- [ ] `cover_applied` 为 `true`，不是 `false` 或 `null`
- [ ] 正文 `--check-only` 的 `errors` 为 0（必须先跑，别绕过）
- [ ] 保存闸门四项 `section / strong / styled / inline-block` 都不低于源文件（出现 `SKIP-SAVE-BAD-BODY` 即失败）
- [ ] 封面宽高比 2.35:1 通过 `cover_check.py`
- [ ] 发到二维码后，截图路径已交给用户
- [ ] 没有替用户点最后那一下
