# course-resource-download

枚举在线课程资料，批量下载允许保存的课件，校验文件并记录每项资源的处理结果。既可作为命令行工具使用，也可作为 Codex skill 使用。

项目提供智慧树共享课、智慧课程与学习通三个实验性适配器。它们使用浏览器访问学生端页面；平台页面和接口可能变化。已在两门授权智慧课程中验证知识主题枚举，并完成 4 个真实 PPTX 的下载、校验、续跑和损坏恢复；学习通适配器已在授权课程完成章节枚举、11 个真实 PPTX 的下载校验与续跑复用验收（2026-10-28）。五组跨平台 CI 全部通过。完整范围与未验证项见 [VALIDATION.md](VALIDATION.md)。

## 支持范围

| 平台 | 当前能力 | 状态 |
| --- | --- | --- |
| 智慧树共享课 `stuonline` | 递归枚举文件夹、检查下载权限、下载文档、保存清单 | 实验性适配器；离线测试 |
| 智慧树智慧课程 `aicourse` | 遍历任务与活动或知识主题目录，收集附件和知识点资源，下载文档 | 实验性；知识主题与 PPTX 下载/恢复已实测，任务布局为离线测试 |
| 学习通 `chaoxing` | 读取课程章节，逐知识点收集文档与视频资源，按章节前缀筛选并下载 | 实验性；真实课程枚举、PPTX 下载与续跑已实测，见 [适配说明](references/chaoxing.md) |
| 中国大学 MOOC / `icourse163` | 历史接口参考 | 尚未实现适配器 |
| 高教社 `abooks` | 历史接口参考 | 尚未实现适配器 |

默认跳过视频。`--include-video` 保留为兼容选项，只改变候选筛选；当前视频和旧版 Office 没有受支持的完整性校验，仍记录为 `skipped_unsupported_format`。不提供 HLS 分片合并。下载类型和校验边界见 [清单与校验约定](references/manifests.md)。

## 安装

要求 Python 3.10 或更新版本。以下命令在克隆或解压后的项目根目录执行：

```sh
python -m venv .venv
```

激活虚拟环境：

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```sh
# macOS / Linux
. .venv/bin/activate
```

```sh
python -m pip install -e '.[browser]'
python -m playwright install chromium
course-resource-download --help
```

只做本地文件校验时，安装核心包即可：`python -m pip install -e .`。浏览器依赖在 `[browser]` 中，验证码图像处理依赖在 `[captcha]` 中，开发工具在 `[dev]` 中。浏览器安装失败时，先解决 Playwright 的系统依赖或网络问题；离线测试不需要登录课程平台。

所有命令均可写成 `python -m course_resource_download <子命令> ...`。

## 快速开始

### 1. 登录

```sh
course-resource-download login --service "<课程页面 URL>"
```

默认打开浏览器，由你完成登录与验证码；默认等待上限为 180 秒，可用 `--timeout` 调整。成功后保存浏览器登录状态，默认路径是运行命令时所在目录的 `.course-resource-download/state.json`。后续命令在同一工作目录运行；如果更换工作目录，使用 `--state "<登录状态文件路径>"`。已有状态默认不会覆盖；确认要更新时传 `--overwrite-state`。

按需使用的凭据解析优先级为环境变量 `ZHS_PHONE` / `ZHS_PWD`、命令行 `--phone` / `--pwd`、`--credentials` 指定的凭据 JSON 文件；不会自动读取仓库中的凭据文件。优先使用交互登录；需要自动填入凭据时可使用环境变量。命令行参数可能出现在历史记录和进程列表中。文件方式的格式与说明见 [智慧树参考](references/zhihuishu.md)。

自动验证码求解只在显式设置 `--auto-captcha` 时启用，需额外安装 `python -m pip install -e '.[browser,captcha]'`。该功能依赖平台行为，不能保证成功。

### 2. 枚举

共享课使用学生端 `stuIndexV2` 页面链接：

```sh
course-resource-download stuonline --url "<共享课页面 URL>" --out ./downloads/course --list-only
```

智慧课程使用课程与班级标识：

```sh
course-resource-download aicourse --course-id COURSE_ID --class-id CLASS_ID --out ./downloads/course --list-only
```

学习通使用学生端课程页链接：

```sh
course-resource-download chaoxing --url "<学习通 stu 课程页 URL>" --out ./downloads/course --list-only
```

各平台标识不能互换。命令生成 `_discovery.json`，保留已有 `_manifest.json`。检查发现的资源、权限限制和遍历失败后再下载。

智慧课程默认 `--layout auto`，等待任务卡后选择任务流程或知识主题流程；也可显式指定 `--layout tasks` 或 `--layout knowledge`。知识主题模式只读取目录及各知识点的资源元数据，不打开学习或播放器页面。

若已在本机浏览器登录，可让智慧课程或学习通命令复用调用方已确认账号与配置的 CDP 会话：

```sh
course-resource-download aicourse --course-id COURSE_ID --class-id CLASS_ID --out ./downloads/course --cdp-endpoint http://127.0.0.1:9222 --list-only
```

`--cdp-endpoint` 只接受本机回环地址，不启动或配置浏览器，也不读取 `--state`。先按所用浏览器的方式启用并授权调试连接，确认连接的是目标账号；工具会新开并关闭自己的标签页，保留浏览器和原有标签页。CDP 可访问登录会话，仅连接可信的本机端点。未使用 CDP 时仍按前面的 `login` 流程操作。

共享课可追加 `--folder "第一章" --folder "第二章/课件"`，只处理所选目录及其后代；参数可重复，目录不存在时记录失败。路径相对课程资料根目录，以枚举清单的目录名为准。学习通可用 `--chapter "2."` 按目录标签前缀限定章节，参数可重复。智慧课程当前不支持任务筛选，不应使用全课下载替代用户指定的部分任务。

### 3. 下载与校验

去掉 `--list-only`，在原输出目录重跑；各适配器用法一致：

```sh
course-resource-download stuonline --url "<共享课页面 URL>" --out ./downloads/course
course-resource-download verify ./downloads/course --recursive
```

下载清单 `_manifest.json` 用于记录状态和续跑。文件先写入临时文件，通过校验后再替换目标。清单也采用临时文件加原子替换。保留原清单有助于识别上次已下载的文件，避免同名资源被反复另存。

适配器退出 `0` 表示本轮处理满足其完成条件；主动跳过的视频、其他不支持格式或锁定任务仍可能存在。退出 `1` 表示失败、无下载权限或遍历不完整等需要处理的情况。参数错误由命令行解析器返回非零。详细状态与校验边界见 [清单约定](references/manifests.md)。

独立的 `verify` 检查本地文件，不会重新访问课程平台；即使文件全部通过，也不能据此证明课程目录完整。不支持格式或没有可校验文件时返回 `1`。建议输出目录只保存课件和工具生成的清单，将人工编写的说明放在其他目录。

### 4. 清理登录状态

登录状态含可复用的 Cookie。任务完成或取消后删除：

```powershell
# Windows PowerShell；如使用自定义 --state，请替换为同一文件
Remove-Item -LiteralPath '.course-resource-download/state.json'
```

```sh
# macOS / Linux
rm -- .course-resource-download/state.json
```

清单可能包含签名下载链接或课程标识。将输出目录保存在私有位置，分享错误报告前脱敏；不要上传登录状态、凭据、课程资料或原始平台响应。详见 [SECURITY.md](SECURITY.md)。

## 作为 skill 使用

将整个项目目录作为 `course-resource-download` skill 安装到代理支持的 skill 目录，并在该目录完成上面的 Python 依赖安装。入口为 [SKILL.md](SKILL.md)，平台差异和排错资料按需加载。项目没有绑定特定用户的浏览器配置、课程库路径或其他本地 skill。

已有调用仍可使用兼容入口：

```sh
python scripts/zh_login.py --help
python scripts/zh_stuonline.py --help
python scripts/zh_aicourse.py --help
python scripts/verify.py --help
```

参数与统一 CLI 保持兼容。默认登录状态路径统一为 `.course-resource-download/state.json`；旧版位于脚本旁的 `zh_state.json` 需通过 `--state` 显式传入。

## 从旧脚本迁移

`0.2.0a1` 将公共文件、下载、清单与锁逻辑集中到可安装的 Python 包；旧脚本保留为转发入口。以下行为变化需要留意：

- 登录改为默认人工完成，自动验证码需 `--auto-captcha` 和 `[captcha]` 依赖。
- 默认状态文件位置改变；已有文件默认拒绝覆盖，需显式 `--overwrite-state`。凭据文件仅通过 `--credentials` 读取。
- 旧版 `.doc` / `.ppt` / `.xls`、视频与音频不再凭文件头或非空大小被视为校验通过，适配器记录为不支持的格式。
- 仅枚举使用 `_discovery.json`，下载记录保留在 `_manifest.json`；旧清单中的可用认领信息可用于续跑，不能复用的记录会重新处理。
- 输出目录锁改用操作系统锁；`.download.lock` 持久存在是正常情况，不要手工删除。

升级前保留原输出目录及清单，升级后先枚举，再在原目录续跑并核对结果。此版本仍是实验性预发布版。

## 开发与测试

```sh
python -m pip install -e '.[dev]'
python -m coverage run -m pytest
python -m coverage report
python -m ruff check .
python -m ruff format --check .
python -m build --sdist --wheel
```

离线测试使用临时目录、模拟浏览器页面及合成文件，不读取真实凭据、不访问课程平台。它们覆盖可重复验证的文件、清单、命令行和适配器行为；不能替代平台改版后的真实课程验收。首次发布前应按照 [CONTRIBUTING.md](CONTRIBUTING.md) 记录授权课程的人工验收结果，明确仍未通过的项目。

本轮验证的具体结果和未执行项见 [VALIDATION.md](VALIDATION.md)，实施范围见 [REFACTOR_PLAN.md](REFACTOR_PLAN.md)。

进一步阅读：[智慧树适配说明](references/zhihuishu.md)、[学习通适配说明](references/chaoxing.md)、[清单与校验](references/manifests.md)、[故障排查](references/troubleshooting.md)、[未实现平台的历史参考](references/platform-notes.md)。

## 许可证

代码以 [MIT License](LICENSE) 发布。课程资料与平台内容的权利归各自权利人；项目许可证不授予课程内容的再分发权。

开发、测试、候选构建和回退约定见[维护流程](MAINTENANCE.md)。
