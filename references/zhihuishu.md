# 智慧树适配说明

本文记录适配器使用方式和维护所需的平台差异。接口、选择器和字段语义来自运行观察，未经平台承诺；平台改版后应重新确认。知识主题模式已在两门真实课程枚举，并完成 4 个 PPTX 的下载、续跑与损坏恢复；任务模式和共享课仍以离线样本测试为主。

## 登录与会话

```sh
course-resource-download login --service "<学生端课程页面 URL>"
```

默认交互登录：浏览器打开后，由用户完成账号登录与验证码。默认会话路径为当前工作目录的 `.course-resource-download/state.json`。下载命令使用相同路径或显式传 `--state`。会话含可复用 Cookie，任务结束后删除。

需要程序填入账号时，凭据解析按单字段依次取环境变量、命令行参数、凭据文件。环境变量名为 `ZHS_PHONE` 与 `ZHS_PWD`；命令行参数为 `--phone` 与 `--pwd`。凭据文件 JSON 格式为：

```json
{
  "phone": "YOUR_PHONE",
  "password": "YOUR_PASSWORD"
}
```

文件通过 `--credentials "<私有 JSON 路径>"` 显式指定；不会自动读取仓库中的 `credentials.json`。不要将真实文件或值粘贴到日志、Issue 或版本控制。手动登录不要求先提供账号密码给工具。

登录仅接受智慧树 HTTPS 页面作为 `--service`。默认等待 180 秒，可用 `--timeout` 调整；验证码和未完成的临时状态文件会自动清理，但成功输出的状态由调用方清理。已有状态不会自动覆盖，重新登录时显式传 `--overwrite-state`。

### 可选自动验证码

```sh
python -m pip install -e '.[browser,captcha]'
course-resource-download login --service "<学生端课程页面 URL>" --auto-captcha
```

历史求解器依赖图像模板匹配：获取验证码原图后，用前景 alpha 作为掩码，在页面当前渲染的面板中匹配缺口，并排除拼图块当前区域。验证码可能刷新或改变，低置信度或重试耗尽应明确失败，不能将此情况当作已登录。该模式为显式启用，默认使用人工验证码；不要因检测到验证码就自动切换求解器。

## 共享课：stuonline

学生端课件页通常包含 `stuIndexV2`，由文件夹和文件表格组成。

```sh
course-resource-download stuonline --url "<共享课 stuIndexV2 URL>" --out ./downloads/course --list-only
course-resource-download stuonline --url "<共享课 stuIndexV2 URL>" --out ./downloads/course
```

维护时留意：

- 历史页面的 `candownload=1` 对学生表示禁止下载，`0` 表示可下载，不能按字段名猜测语义。当前适配器据此记录权限拒绝；页面改版需核对实际按钮与授权行为。
- 文件夹行上的点击可能触发右键菜单。历史入口是文件夹内的 `a.teacher-file-a` 与页面的 `openFolder`，不能将任意行点击成功当作已进入目录。
- 重新导航并重放文件夹路径时，要检查登录重定向和文件表格是否存在；读不到表格不能当作空目录。
- 下载直链来自页面所使用的 `downloadMaterial1` 响应。历史 `filePreviewIframe1(...)` 第三个参数是数据标识。页面 `jsonPath` 与接口响应均需按运行时数据读取。
- 文件夹路径或资源名不是可信本地路径。由公共工具生成安全文件名，并对同名不同来源进行区分。

每个访问目录应在清单 `folders` 中留下成功或失败记录。单个目录失败可保留其他目录结果，但整轮不能报告为完整成功。

## 智慧课程：aicourse

智慧课程学生端与共享课是不同系统，课程标识不能混用。学生端路径历史形态：

```text
/singleCourse/knowledgeStudy/{courseId}/{classId}
/projectTaskDetail/{courseId}/{classId}
```

```sh
course-resource-download aicourse --course-id COURSE_ID --class-id CLASS_ID --out ./downloads/course --list-only
course-resource-download aicourse --course-id COURSE_ID --class-id CLASS_ID --out ./downloads/course
```

`--layout auto` 默认给任务卡一个有界响应窗口，再选择任务流程或知识主题流程；知识主题请求可能先于任务卡到达，不能一看到它就判定没有任务。可用 `--layout tasks` 或 `--layout knowledge` 强制布局。

### 知识主题布局

读取公开前端入口引用的版本化 `courseUrl-*.js`，在当前登录页面内调用其 `courseKnowledgeDic` 与 `getListNodeResourcesWithStatus` 读取方法。资源请求逐知识点进行，单次请求超时为 30 秒；每个节点保留终态和检查点。只把所需标识、名称、类型、链接及明确的下载许可字段传回 Python，不持久化完整响应或学习进度。前端模块路径或导出方法变更时明确失败。

目录结构为 `themeList[].subThemeList[]`，知识点来自各层的 `knowledgeList[]`。资源位于 `resourceList[].resourcesDetail`。`resourcesDataType=22` 且同一行有 `resourcesVideoDetail` 时，`resourcesUrl` 可能只是多个视频共用的 JPG 封面；不能将它作为视频直链或去重身份。电子书与视频按既有规则记录跳过。`canDownload` 或 `downloadAllowed` 明确为布尔 `false` 时记录权限拒绝。

此流程不导航到学习/播放器路由，不调用学习进度写入接口。直接打开知识点学习页会自动播放视频，不适合作为文档枚举手段。

已登录浏览器可通过 `--cdp-endpoint http://127.0.0.1:9222` 接入，支持回环 HTTP(S)/WS(S)，拒绝远程主机、内嵌凭据、查询参数与片段。调用方负责启用调试并确认账号和 profile；本工具不启动、复制或修改用户 profile。使用已有浏览器上下文新建页面，初始化脚本仅作用于该页面；结束或出错均关闭自己创建的标签页，保留用户浏览器。此模式无需保存 Cookie 到状态文件，也无需安装 Playwright Chromium；仍需 `[browser]` Python 依赖。

### 任务布局

遍历结构为课程 → 任务 → 活动 → 知识点 → 资源；任务还可能带独立附件。维护时留意：

- `get-course-mapUid` 的响应提供图谱标识，勿凭课程标识自行拼接。
- 任务详情每次显示一个活动。翻页箭头到末页可能只加 CSS 类，浏览器点击调用仍返回成功；必须检查禁用类和响应身份。
- 历史活动响应的身份键是 `(documentId, taskId)`；其中 `documentId` 属任务，响应 `taskId` 属活动。活动名可能重复，响应数增加也可能是上一页迟到响应，两者均不适合作为翻页成功依据。
- `totalActivities=0` 是有效的空任务。锁定任务的 `canClick=false` 或非空 `lockReason` 应在翻页前识别，不能反复等待直到超时后再推断锁定。
- 声明的活动数超过 `--max-activities` 或翻页后没看到新的身份时，记录 `incomplete`，保留已收集结果并返回失败。
- `knowledgeDetailList[].resourcesList[]` 是知识点资源；历史类型标签 `11` 为文档、`21` 为电子书、`22` 为视频。类型标签与实际扩展名还需共同检查。
- 任务附件历史上位于 `teachTasks[].resources[]`。仅遍历知识点资源会遗漏附件。
- `/course/*` 曾为教师端路由；不要把学生访问空白页判作课程没有资料。

历史学生首页的 AI 课程列表请求名为 `queryStudentAICourseList`，使用 POST，响应含课程、班级和链接字段。仅在用户需要发现课程标识且现有入口不足时调查该流程；它并非当前 CLI 的独立列课命令。

以上字段仅作为适配器维护线索。不要通过构造未授权请求、解锁任务或改写平台权限扩大用户原有访问范围。

## 视频下载

`--include-video` 会下载 `.mp4` / `.m4v` 视频。其他容器（`.mkv`、`.avi`、`.mov`、`.wmv` 等）仍记 `skipped_unsupported_format`，因为没有对应校验器，不能算作已验证。以下为 2026-10-29 在智慧课程学习页（`/learnPage/{courseId}/{classId}/{nodeUid}?mapUid=...`）实测的观察，平台改版需重新确认。

1. **资源清单**来自 `POST https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/resources/list-knowledge-resource`。请求体是加密的 `{secretStr, date}`，不能离线重放；适配器在已登录页面内调用。`data.resourceList[].resourcesDetail` 给出 `resourcesName`、`resourcesUrl`、`resourcesSize`、`resourcesTime`（秒）、`resourcesSuffix`、`resourcesFileId`。
2. **码流回退。** `resourcesUrl` 指向原始上传，CDN 经常回源失败（源站 403，对外表现为 `424 MirrorFailed`）。平台播放器自己请求的是 `<hash>_512.mp4` 转码流，所以适配器按「原始 → `_512`」顺序尝试，命中转码流时在清单里记 `variant: transcoded`。两条地址权限相同，原始被拒时转码流同样被拒，因此这不是绕过权限；`_512` 也失败时按失败记录，不要另找接口。`_1024/_2048/_1080/_720/_480` 等后缀均为 `404`，`newbase.zhihuishu.com/video/initVideoNew?videoID=<resourcesFileId>` 也只返回「校内」一条线，与 `_512` 一致。
3. **请求头。** 下载必须带 `Referer: https://ai-smart-course-student-pro.zhihuishu.com/`（适配器已带）和桌面 UA；缺 Referer 直接 `403`。不需要 Cookie。
4. **校验。** MP4 走 ISO base media 容器校验：首个箱必须是 `ftyp`，且存在 `moov` 与 `mdat`，箱链不得越界。这能发现截断传输和把 HTML 错误页存成 `.mp4` 的情况，但**不解码媒体流、不核对时长**。需要时长证据时用 `ffprobe` 比对 `resourcesTime`（容差约 2 秒）。
5. **画质。** `_512` 是 1200x720 或 1152x720、约 280 kbps，明显低于原始码流；交付时要说明这一上限。
6. **副作用。** 适配器不进入播放器路由。人工在页面点击知识点会自动播放并可能写入学习进度，抓完立即 `document.querySelector('video').pause()`。

不要把 `resourcesUrl` 当作视频的唯一身份：同一行有 `resourcesVideoDetail` 时它可能只是多个视频共用的 JPG 封面。

## 目录范围与就绪确认

共享课 `--folder` 可重复，匹配相对课程资料根目录的完整目录标签，并包含其后代；缺失目录记失败。先无筛选枚举可查看完整目录标签。智慧课程没有任务筛选参数。共享课进入目录后等待表格资源签名变化，超过时限仍相同就保守记失败；父子目录内容完全一致时需要人工核对。

共享课只允许明确的 `candownload=0` 文档进入下载接口；`1` 为禁止，未知值记录失败。默认排除的视频和非文档不属于本次下载候选。浏览器保留了历史实测的 AutomationControlled/ webdriver 初始化兼容措施，当前真实平台有效性尚未重新验收。
