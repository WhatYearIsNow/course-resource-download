# icourse163（中国大学 MOOC）适配器

`icourse163` 命令处理 `icourse163.org` 上的 MOOC 与 SPOC 课程，下载**文档**与**视频**。课程标识取自课程学习页 URL：`/learn/<courseKey>?tid=<termId>`、`/spoc/learn/<courseKey>?tid=<termId>`、`/mooc/learn/<courseKey>/content?tid=<termId>` 三种形态都接受。

页面结构与接口字段是**观测行为**，不是平台公开文档；平台改版会使适配器失效。

## 登录

平台账号登录含验证码，适配器不做表单填入。两种方式：

- `login --service "<课程学习页 URL>"`：打开浏览器由用户手动登录，会话写入 `--state`。
- `--cdp-endpoint ws://127.0.0.1:9222`：复用调用方已确认账号与 profile 的本机浏览器。工具只关闭自己创建的标签页。

登录态失效时页面会跳到 `passport.icourse163.org` 或 `/login`，适配器报 `Login state expired`。

## 课程树

`POST /web/j/courseBean.getLastLearnedMocTermDto.rpc`（表单 `termId=<termId>`，需 `edu-script-token` 头，值等于 `NTESSTUDYSI` Cookie）返回 `result.mocTermDto.chapters`：

- `chapters[].lessons[].units[]`：单元，`contentType` 1=视频、3=文档、5=章节内检测题。
- `chapters[].quizs[]`、`chapters[].homeworks[]`：章节测验与作业。

**测验、作业、检测题只记为遍历记录，不下载、不打开任何测验路由。** 章节测验通常只有 2 次机会，打开即可能消耗一次。

## 资源身份

清单记录 `docid:<contentId>` / `videoid:<contentId>`，**不记录真实直链**：视频播放地址带签名 `ak`，会过期。

## 文档

`GET /front-mooc-learn/api/learn/content/unit-page?courseKey=&termId=&lessonId=&unitId=` 返回 `result.detail.mediaUrl`（回退 `downloadUrl`、`pdfPlayback.resourceUrl`）与 `detail.type`。

- MOOC 的文档直链通常以 `.pdf` 结尾；**SPOC 的文档直链没有扩展名**（形如 `.../D3E96F9E...-15439870`），扩展名只能取自 `detail.type`。
- 枚举阶段按 `.pdf` 归类；下载时若 `detail.type` 给出别的类型，文件名会按实际扩展名改正。

## 视频

视频只有 HLS，没有 MP4 直链，需要两步解析：

1. `POST /web/j/resourceRpcBean.getResourceTokenV2.rpc`，表单 `bizId=<unitId>&bizType=1&contentType=1&sign=&timestamp=<毫秒>`，返回 `videoSignDto.videoId` 与 `signature`。
2. `GET https://vod.study.163.com/eds/api/v1/vod/video?videoId=&signature=&clientType=1`，返回 `result.videos[]`，每项含 `quality` 与 `videoUrl`（m3u8）。适配器取 `quality` 最高的一路。

`sign` 参数当前不被服务端校验，适配器传空值；这是观测到的现状，不是绕过权限——能否取到仍然取决于账号对该单元的访问权。

拿到 m3u8 后由 `hls.download_hls` 处理：按播放列表顺序下载分片并拼接成 MPEG-TS 暂存文件，再用 ffmpeg `-c copy` 无损转封装成 MP4，最后过 `check_mp4` 容器校验才原子替换目标文件。

### ffmpeg 依赖

视频下载**需要 ffmpeg**。查找顺序：环境变量 `CRD_FFMPEG` → `PATH`。缺失时视频行记 `failed` 并给出可操作报错，文档下载不受影响；不会降级成未校验的文件。

`--max-video-mb`（默认 4096）限制单个视频拼接后的总字节数，超限即失败。

## 章节筛选

`--chapter "第12章"` 按章名前缀筛选，可重复。未选中的章节记 `Excluded by --chapter selection`。

## 章号陷阱

同一批内容在不同来源有**三套编号**，引用时必须说清是哪一套：

| MOOC 平台 | 上课用 PPT | 教材 |
| --- | --- | --- |
| 第12章 标准件与弹簧、齿轮 | 第6章 标准件与常用件 | 第9章 标准件与常用件 |
| 第13章 零件图 | 第7章 零件图 | 第10章 零件图 |
| 第14章 装配图 | 第8章 装配图 | 第11章 装配图 |

## 维护提示

- 课程树接口与单元页接口属于不同前端（`/web/j/*.rpc` 与 `/front-mooc-learn/api/*`），任一侧改版都要重新核对。
- 单元页接口必须带 `lessonId`，只给 `courseKey` 与 `termId` 会返回 400。
- 播放器路由会触发自动播放；适配器只访问学习页并用页面内 `fetch` 取数据，不进入播放器或测验路由。
