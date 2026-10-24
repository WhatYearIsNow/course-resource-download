# 未实现平台：历史接口线索

以下信息来自旧版 skill 的操作记录，尚未对当前平台重新验证，也没有对应 CLI 下载适配器。它们只供未来调研和实现时定位，不能作为“已支持”或“已测试”的证据。

## 中国大学 MOOC / icourse163

历史课程数据使用 DWR 接口。曾观察到：

- `courseBean.getLastLearnedMocTermDto.rpc` 返回课程树相关数据。
- `courseBean.getLessonUnitLearnVo.dwr` 的文档单元含 `textUrl` 与 `textPages`。
- `textUrl` 可能是带签名的下载链接，不能假定永久有效。

未来实现应先确认课程树、文档单元的可访问范围与真实下载权限，再分别测试枚举、签名链接失效和文件验证。不要把视频单元数量当作课件数量，也不要宣称现有智慧树适配器能处理该平台。

## 高教社 abooks

历史记录中的接口包括：

- `/api/v3/project/findCatalog/{projectId}`：项目目录。
- `/api/v2/resource/{id}?projectId=...`：曾返回含 `oss.downloadUrl` 的资源信息。
- `/api/v3/resource/free/{id}`：曾仅提供预览链接，不能直接等同于原始文件下载。

历史登录依赖 `id-token` Cookie。未来实现时应重新确认接口、授权与签名链接，不在请求日志中暴露令牌，不把仅可预览资源的存在当作可下载授权。

## 新适配器完成条件

实现可执行入口、独立发现清单、资源身份与权限判定、遗漏和失败记录，并复用公共原子下载和格式校验。补充合成离线样本、失败路径测试和明确标注日期的授权课程人工验收后，再调整 README 的支持矩阵。
