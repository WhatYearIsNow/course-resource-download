# 变更记录

## 未发布

- 修复共享课 `stuonline` 读到空课件表：课件表格由 AJAX 填充，`domcontentloaded` 时只有表头一个 `tr`，原实现据此把目录记成 `ok` 却枚举出 0 个资源。现在等数据行数量稳定后再读取，空目录稳定在 0。
- 共享课 `--include-video` 现在能识别视频：视频行的 `onclick` 是 `tm_data_show_proxy(this, opid, '<容器>', '<扩展名>', '<直链>', …)`，直链在参数里，原实现按文档的 `filePreviewIframe1` 解析，一律记 `Cannot parse resource download id`。`.mp4`/`.m4v` 进入下载候选并走 MP4 容器校验，`.avi` 等仍记 `skipped_unsupported_format`；`--list-only` 下视频只分类不下载。
- 文档：`references/zhihuishu.md` 记录 AJAX 表格等待与视频行结构；`VALIDATION.md` 补充共享课真实课程验收（含平台侧视频文件已 404 的边界）。
- 新增 MP4 容器校验器：`.mp4` / `.m4v` 按 ISO base media 结构校验（`ftyp` 首箱、`moov`、`mdat`、箱链不越界），能发现截断传输和把 HTML 错误页存成 `.mp4` 的情况；不解码媒体流，也不核对时长。
- 智慧课程 `--include-video` 现在会下载视频：`.mp4` / `.m4v` 进入下载候选，其他媒体容器仍记 `skipped_unsupported_format`。
  - 平台原始码流经常回源失败（`424 MirrorFailed`），适配器按「原始 → `_512` 转码流」顺序尝试，命中转码流时在清单记 `variant: transcoded`。两条地址权限相同，不扩大可下载范围。
  - 资源身份仍是原始 URL，续跑能识别已下载文件。
- 文档：`references/zhihuishu.md` 增加「视频下载」一节，记录码流回退、`Referer` 要求、校验边界与 `_512` 的画质上限；`references/manifests.md` 补充 MP4 校验能力与 `variant` 字段。

## 0.2.0 — 2026-10-29

- 新增学习通（Chaoxing）课件下载适配器 `chaoxing` 子命令：从学生端课程页读取章节与运行时账号参数，逐知识点打开学习页，在同源上下文收集 Ananas 文档对象并下载平台允许保存的文档。
  - 资源身份为平台 `objectid`；跨章节重复文档去重并记录全部位置。
  - 清单只记录稳定对象标识，不写入易变签名直链；无下载直链记 `permission_denied`。
  - `--chapter "2."` 按目录标签前缀筛选；不可点击目录条目记 `locked`，不视为整轮失败。
  - `login` 接受 `*.chaoxing.com` 服务地址（纯人工登录）；适配器支持回环 `--cdp-endpoint`。
- 修复校验器误报：OOXML `r:id=""` 空引用（导航超链接合法模式）不再判为悬空引用，附回归测试。
- 固定开发与构建工具版本，补充维护指南、候选清单，并把格式检查接入 CI。

## 0.1.0

- 首个公开版本：智慧树两个系统的课程文档发现、下载与校验，原子写入、锁、续跑与清单。
