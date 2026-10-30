# 学习通（Chaoxing）适配说明

本文记录 `chaoxing` 子命令使用方式和维护所需的平台差异。页面结构、接口与字段语义来自运行观察（2026-10-27/28），未经平台承诺；平台改版后应重新确认。

## 登录与会话

```sh
course-resource-download login --service "<学习通课程页面 URL>"
```

登录命令接受 `*.chaoxing.com` 的 HTTPS 服务地址。学习通登录不做任何表单填入：浏览器打开服务页后由平台跳转到通行证，由你完成登录与验证码，页面回到课程地址即视为成功。`--phone` / `--pwd` / `--credentials` / `--auto-captcha` 只适用于智慧树，对学习通会直接报参数错误。

会话保存与清理约定与智慧树一致：默认 `.course-resource-download/state.json`，任务结束后删除；已有状态需显式 `--overwrite-state` 才覆盖。

与智慧课程相同，`chaoxing` 命令也支持 `--cdp-endpoint` 附加到调用方已确认账号的本机浏览器（仅回环地址），工具只新开并关闭自己的标签页。

## 枚举与下载

```sh
course-resource-download chaoxing --url "<学习通 stu 课程页 URL>" --out ./downloads/course --list-only
course-resource-download chaoxing --url "<学习通 stu 课程页 URL>" --out ./downloads/course
course-resource-download chaoxing --url "..." --out ./downloads/course --chapter "2."
```

- `--url` 必须是学生端课程页（通常为 `mooc2-ans.chaoxing.com/.../mycourse/stu`），且包含数字 `courseid` 与 `clazzid` 参数。
- 适配器从页面渲染后的目录读取章节与每账号参数（`enc` 等运行时读取，不硬编码），逐节点打开与正常点击一致的学习页，再在同源上下文读取知识卡片页中的文档对象标识（`objectid`）与对象状态。
- `--chapter` 按目录标签前缀过滤（大小写不敏感，可重复），如 `--chapter "2."` 限定某章及全部子节点；未选中的节点记录在 `tasks` 中并注明排除原因。
- 目录中不可点击的条目（章节标题或锁定的知识点）没有知识标识，记录为 `tasks` 中的 `locked`，不视为整轮失败，交付时应说明。

## 资源身份与状态

- 资源身份是平台文档对象的 `objectid`；同一文档出现在多个章节时只处理一次，`chapters` 字段列出全部出现位置。
- 清单中的 `url` 字段记录 `objectid:<hex>` 稳定标识，**不写入签名下载直链**；签名链接每次读取都会变化，只在本次运行内存中短暂存在。
- 对象状态无下载直链时记录为 `permission_denied`（平台未暴露下载），整轮非零退出。
- 类型按平台文件名扩展名判定。视频默认 `skipped_video`；加 `--include-video` 后 `.mp4`/`.m4v` 正常下载并走 ISO base media 容器校验，其他媒体容器仍记 `skipped_unsupported_format`。
- 签名直链要求 `Referer: https://mooc1.chaoxing.com/`，缺失会得到 403 页面；公共下载逻辑已按此携带。

## 视频

学习通的视频**就是普通 MP4**，和文档走同一条链路，不需要 HLS 或 ffmpeg：

- 同一个 `ananas/status/<objectid>` 响应里，`filename` 以 `.mp4` 结尾、并带 `duration` / `dtoken` 字段的就是视频；`download` 字段给出签名直链。
- 加 `--include-video` 后按 `.mp4`/`.m4v` 下载，校验走 MP4 容器检查（`ftyp` 首箱、存在 `moov` 与 `mdat`、箱链不越界），不解码媒体流也不核对时长。
- 签名直链同样要求 `Referer: https://mooc1.chaoxing.com/`，缺失会拿到 403 页面；适配器已携带。
- 2026-10-27 首次遇到真实视频（`How to Write a Killer resume.mp4`，19.8 MB），当时按默认跳过；2026-10-30 起 `--include-video` 可下载。

## 维护时留意

- 学习页与知识卡片页是同源接口读取；不要从其他域名的页面直接跨域请求。
- 每个知识节点访问一次学习页；与智慧树知识主题模式不同，本平台没有可用的"仅元数据"接口，因此无法完全避开学习路由。适配器在工具自有页面禁用媒体自动播放。
- 目录不可点击条目同时可能是"未开放"内容；当前无法与章节标题区分，统一记录为 `locked` 并在交付中说明。
- 会话过期表现为页面跳转到 `passport2.chaoxing.com`，适配器据此中断并明确报错，不会把登录页内容当作课程数据。
