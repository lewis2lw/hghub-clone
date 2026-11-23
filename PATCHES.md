# PATCHES.md — HGhubX Web 版（方案 B）对上游源码的改动记录

- 基线：HGhubX v2.2.8（本工作副本，`~/workspace/hghubx-src`，git 基线提交 `ccf80c3`）。
- 目的：把 React 前端跑在普通浏览器里（标准版，不要 TV 版），由我们自己的 Python 后端
  （8081 同源）提供数据；前端原来调 Tauri Rust 后端，全部业务请求走
  `@tauri-apps/api/core` 的 `invoke(cmd, args)`，Python 后端提供 `POST /api/tauri`
 （JSON `{ cmd, args }`，同源）作为等价分发口。
- 原则：只改实现 Tauri 宿主依赖所必需的地方；不改业务逻辑，不改 `src-tauri/`，
  不改任何命令名、参数名与 JSON 形状。后续同事凭本文件可复现全部改动。
- 构建命令（标准版）：`pnpm install --frozen-lockfile`，然后 `pnpm build`
  （三次 `tsc` + 一次 Vite 构建，见 `AGENTS.md`）。

## 0. Tauri 专属 import 全量清单（改动前 grep 结果）

`grep -rn "@tauri-apps" src/` 共 21 个文件，无一漏网；另有两处非 import 的 Tauri 耦合
（见 §0.1）。入口 `src/main.tsx`、`src/editionEntry.ts`、`src/app/App.tsx` 已重点核对：
`editionEntry.ts` 无 Tauri import；`main.tsx` 只有 `invoke`；`App.tsx` 只有
`addPluginListener` + `invoke`。

### 0.1 `@tauri-apps/api/core`（18 个文件）

| 文件 | 用到的符号 | 用途 |
| --- | --- | --- |
| `src/main.tsx:5` | `invoke` | `debug_log`（全局 error / unhandledrejection 上报） |
| `src/shared/api/api.ts:1` | `invoke` | 全部业务命令（`api_*`、`app_boot`、`line_*`、`check_update` 等）与 `logLine`（`debug_log`）；另有 `clearCache`（`cache_clear`）、`netDiag`（`net_diag`）直接调 `invoke` |
| `src/shared/diagnostics/playbackDiagnostics.ts:1` | `invoke` | `playback_log_status` / `playback_log_set` / `playback_log_write` / `playback_log_read` |
| `src/app/App.tsx:7` | `addPluginListener`、`invoke` | 安卓返回键监听（`app` / `back-button`）；`set_text_interaction`（仅 iOS）；`plugin:app\|exit`（回首页后退出）；`debug_log`（仅 DEV 自检） |
| `src/app/startup/LaunchAnimation.tsx:2` | `addPluginListener` | 安卓返回键监听（`app` / `back-button`），用于启动动画阶段返回即完成 |
| `src/shared/ui/UsageAgreement.tsx:2` | `addPluginListener`、`invoke` | 安卓返回键监听（`app` / `back-button`）；`plugin:app\|exit`（拒绝协议后退出） |
| `src/features/feed/Feed.tsx:4` | `invoke` | `set_orientation`（横屏视频进全屏后补发横屏方向请求） |
| `src/features/feed/useFeedViewport.ts:2` | `invoke` | `set_orientation`（全屏横竖屏切换） |
| `src/features/playback/mediaTransport.ts:1` | `invoke` | `media_http_origin`（仅 iOS 原生播放路径，见 §4 缺口 3） |
| `src/features/playback/useFsOrientation.ts:2` | `invoke` | `set_orientation`（退出全屏恢复竖屏） |
| `src/features/playback/useInlinePlayerFullscreen.tsx:2` | `invoke` | `set_orientation`（进出全屏横竖屏切换） |
| `src/shared/lib/clipboard.ts:1` | `isTauri` | 判断是否走 Tauri 剪贴板插件（Web 版恒为 false，见 P4） |
| `src/tv/TvApp.tsx:2` | `invoke` | `plugin:app\|exit`（TV 版退出；标准版构建不走此入口） |
| `src/tv/TvPinGate.tsx:2` | `invoke` | `plugin:app\|exit`（TV 版；同上） |
| `src/tv/library.ts:1` | `invoke` | `tv_migration_complete`（TV 版迁移完成上报；同上） |
| `src/tv/remote.ts:2` | `addPluginListener` | 安卓返回键监听（`app` / `back-button`，TV 版遥控器） |
| `src/tv/updater.ts:2` | `Channel`、`invoke` | `download_tv_update`（`onProgress` 用 `Channel` 传进度，见 §4 缺口 1）；`cancel_tv_update` |

### 0.2 `@tauri-apps/api/window`（3 个文件）

| 文件 | 用到的符号 | 用途 |
| --- | --- | --- |
| `src/app/shell/DesktopChrome.tsx:3` | `getCurrentWindow` | `isMaximized()` / `isFullscreen()` / `onResized(sync)` / `toggleMaximize()` / `minimize()` / `close()`（桌面自绘标题栏） |
| `src/app/shell/ResizeHandles.tsx:1` | `getCurrentWindow` | `startResizeDragging(dir)`（无边框窗口边缘拖拽缩放） |
| `src/shared/lib/fullscreen.ts:1` | `getCurrentWindow` | `setFullscreen(bool)`，但 `setTauriWindowFullscreen()` 先检查 `window` 上有无 `__TAURI_INTERNALS__`，Web 下直接返回，实际调不到窗口方法 |

### 0.3 `@tauri-apps/plugin-clipboard-manager`（1 个文件）

| 文件 | 用到的符号 | 用途 |
| --- | --- | --- |
| `src/shared/lib/clipboard.ts:2` | `writeText` | `copyText()` 的 Tauri 分支 |

### 0.4 非 import 的 Tauri 耦合（未改动，原因见 §3）

- `src/shared/lib/fullscreen.ts:53`：`("__TAURI_INTERNALS__" in window)` 守卫。Web 下恒为
  false，`setTauriWindowFullscreen()` 直接返回，无需改动。
- `src/shared/lib/html.ts:26`：`isInternalWebviewHost()` 把 `tauri.localhost` /
  `hg.localhost` / `ipc.localhost` 当作内网主机（正文链接安全检查）。这是内容安全
  判断，不是媒体基址，与 `LOCAL` 改动无关，未改动。
- `src/shared/api/api.ts` 原注释与 `src-tauri/src/linux_http.rs` 的 `17650` 端口、`src-tauri`
  全部 Rust 代码：Web 版不再使用，未改动。
- `data-tauri-drag-region` 属性（`DesktopChrome.tsx:36/38/46`）：普通浏览器下只是无意义的
  HTML 属性，无副作用，未改动。
- 文案 `"基于 Rust + Tauri 2 构建的跨平台客户端…"`（`Settings.tsx:426` 及 `locales/` 翻译）：
  仅为展示文案，未改动。

## 1. 改动清单

### P1. 新增垫片模块（3 个新文件，`src/` 下）

| 文件 | 行数 | 内容 |
| --- | --- | --- |
| `src/tauriShim.ts` | 67 | 替代 `@tauri-apps/api/core`：`invoke` / `isTauri` / `Channel` / `addPluginListener` |
| `src/tauriWindowShim.ts` | 72 | 替代 `@tauri-apps/api/window`：`getCurrentWindow` / `getAllWindows` |
| `src/tauriClipboardShim.ts` | 32 | 替代 `@tauri-apps/plugin-clipboard-manager`：`writeText` |

#### P1-a `src/tauriShim.ts`：`invoke(cmd, args)` → `fetch POST /api/tauri`

- 改前：`invoke` 是 Tauri 的 IPC 调用（Rust 命令分发）。
- 改后：`fetch("/api/tauri", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cmd, args: args ?? {} }) })`，返回解析后的 JSON（`await res.json()`，泛型 `<T>` 保持不变）。
- 失败行为：`fetch` 本身抛错（断网等）→ 抛 `Error("invoke ${cmd} failed: ${detail}")`；
  HTTP 非 2xx → 读出最多 200 字的响应体，抛 `Error("invoke ${cmd} failed: HTTP ${status}...")`。
  错误信息带 cmd 名，便于调试；与 Tauri「未知命令 reject」相近（Tauri 抛字符串，本垫片抛
  Error——调用点普遍是 `.catch(() => {})` 或 `api.ts` 的 `call()`，后者对非字符串错误有
  `tr("请求失败，请检查网络")` 的兜底，行为一致）。
- `isTauri()`：改后恒返回 `false`。
- `addPluginListener(plugin, event, handler)`：兼容 stub。注册后返回
  `Promise<{ unregister: () => Promise<void> }>`，`unregister` 为空 async 函数；事件永不触发，
  不抛错。调用点形态不变（`.then(handle => handle.unregister())` / `.catch(() => {})` 均可继续工作）。
- `Channel<T>`：兼容 stub。`constructor(onmessage?)` 不抛错，`onmessage` 属性可挂，
  `id` 为自增整数（序列化时 `JSON.stringify` 会丢掉函数属性，只剩 `{ id }`，可安全地放进
  `invoke` 的 `args` 里）。Web 版不走流式通道，回调永远收不到数据；依赖它的调用点见 §4 缺口 1。

#### P1-b `src/tauriWindowShim.ts`：`getCurrentWindow()` → no-op 窗口对象

- 改后：`getCurrentWindow()` 返回单例对象，各方法均为 no-op async 函数：
  `setTitle` / `show` / `hide` / `close` / `minimize` / `maximize` / `unmaximize` /
  `toggleMaximize` / `setFullscreen` / `setVisible` / `center` / `requestUserAttention` /
  `setSize` / `setPosition` / `startResizeDragging`；
  查询类返回浏览器下的合理默认值：`isFullscreen()` → `false`、`isMaximized()` → `false`、
  `isMinimized()` → `false`、`isVisible()` → `true`；
  `onResized(handler)` → `Promise<() => void>`，直接返回空取消函数（浏览器窗口变化不走 Tauri 事件）。
- 另附 `getAllWindows()`（返回 `[getCurrentWindow()]`），源码未使用，补齐以防后续调用点引用。
- 实际调用点覆盖：`DesktopChrome.tsx` 的 `isMaximized` / `isFullscreen` / `onResized` /
  `toggleMaximize` / `minimize` / `close`；`ResizeHandles.tsx` 的 `startResizeDragging`；
  `fullscreen.ts` 的 `setFullscreen`（实际因 `__TAURI_INTERNALS__` 守卫调不到）。

#### P1-c `src/tauriClipboardShim.ts`：`writeText(text)` → 浏览器剪贴板

- 改后：优先 `navigator.clipboard.writeText(text)`；若 API 不可用（非安全上下文的普通 http
  访问、旧 WebView）或调用被拒，`catch` 后回退到 `textarea + document.execCommand("copy")`
  （`execCommand` 返回 false 时抛错）；无 `document` 的环境直接抛错。

### P2. `vite.config.ts`：加三条 `resolve.alias`（第 24–45 行）

- 改前：`resolve.alias` 只有 `@edition-bootstrap` 一条（标准版 → `./src/main.tsx`）。
- 改后：在同一 `alias` 对象内新增三条（原有 `edition` 逻辑、`legacyCss` 等配置均未动）：

```ts
"@tauri-apps/api/core": fileURLToPath(
  new URL("./src/tauriShim.ts", import.meta.url),
),
"@tauri-apps/api/window": fileURLToPath(
  new URL("./src/tauriWindowShim.ts", import.meta.url),
),
"@tauri-apps/plugin-clipboard-manager": fileURLToPath(
  new URL("./src/tauriClipboardShim.ts", import.meta.url),
),
```

- 注意：alias 只影响 Vite 构建（`pnpm dev` / `pnpm build` 的模块解析）；`pnpm build`
  的三次 `tsc --noEmit` 仍解析 `node_modules` 里的真实 `@tauri-apps/*` 包做类型检查，
  所以垫片只需满足运行时调用形态，不必与真实包的类型声明逐字一致。真实包仍保留在
  `package.json` 依赖里（未删），以保证 `pnpm install --frozen-lockfile` 与 `tsc` 不变。

### P3. `src/shared/api/api.ts` 第 33–39 行：`LOCAL` 改成同源

- 改前（第 33–42 行附近）：

```ts
export const LOCAL =
  isMacos || isIos ? "hg://localhost" : isLinux ? "http://127.0.0.1:17650" : "http://hg.localhost";
```

- 改后（第 33–39 行）：

```ts
export const LOCAL = window.location.origin;
```

- 注释同步改写：说明上游 Tauri 时按平台分流（`hg://localhost` / `http://hg.localhost` /
  `http://127.0.0.1:17650`，Rust 侧 `media::LOCAL_ORIGIN`），Web 版由同源 Python 后端
  （8081）提供 `/img`、`/hls`、`/seg`，所以基址就是当前页面 origin。
- 效果（`cover()` / `playlist()` 本体未改）：`cover(url)` → `${origin}/img?u=…`，
  `playlist(url)` → `${origin}/hls?u=…`；`selfTest()` 与 `mediaTransport.ts` 的
  `playbackSource()` 用的同一 `LOCAL`，自动跟随同源。`isMacos` / `isIos` / `isLinux` 等
  UA 判断仍保留（别处还在用，如 `platform` 与播放分支），只不再参与媒体基址计算。

### P4. `src/shared/lib/clipboard.ts`：Web 分支走垫片的 `writeText`（第 4–11 行）

- 改前：非 Tauri 分支直接调 `navigator.clipboard.writeText`，API 不存在时抛
  `tr("当前环境不支持复制，请在应用中重试")`。
- 改后：非 Tauri 分支改为 `await writeText(text)`（即 P1-c 的垫片实现），先走
  `navigator.clipboard`，失败时自动回退 textarea + `execCommand("copy")`。
  `isTauri()` 分支原样保留（Web 下恒为 false，走不到）；随之删除了不再使用的
  `import { tr } from "../i18n"`（`tsc` 的 `noUnusedLocals` 虽为 false，仍清理掉）。
- 改动原因：`isTauri()` 在 Web 版恒为 false，若 Web 分支不走垫片，P1-c 的回退逻辑
  永远执行不到；普通 http 局域网访问时复制会直接失败。

## 2. 未改动的文件（明确说明，避免误以为漏改）

- 全部 21 个 import Tauri 的业务文件（§0 清单）：import 语句原样保留，构建期由 alias
  替换为垫片；业务代码一行未动。
- `src-tauri/`、`src/tv/`、`index.html`、`package.json`、`pnpm-lock.yaml`：未改动。
- `src/shared/lib/fullscreen.ts`、`src/shared/lib/html.ts`：见 §0.4，未改动。

## 3. 复现步骤

```sh
cd ~/workspace/hghubx-src
pnpm install --frozen-lockfile
pnpm build        # 标准版；不要加 VITE_APP_EDITION=tv
```

复现单处改动时，按 §1 的 P1–P4 逐条对照：新建 3 个垫片文件 → `vite.config.ts` 加 3 条
alias → `api.ts` 的 `LOCAL` 改为 `window.location.origin` → `clipboard.ts` 的 Web 分支
改为调垫片 `writeText`。

## 4. 已知缺口（stub 化后的功能降级）

1. **TV 更新下载进度（`src/tv/updater.ts:184`，仅 TV 版）**：`download_tv_update` 的
   `onProgress` 用 `new Channel<TvUpdateProgress>(...)` 传进度；Web 垫片的 Channel 回调
   永不触发。标准版构建不走 TV 入口，无影响；TV 版 Web 化时下载进度条会一直停在初始状态，
   直到 `invoke` 本身 resolve/reject。不是静默假装有数据——进度回调明确收不到。
2. **安卓硬件返回键**：`App.tsx` / `LaunchAnimation.tsx` / `UsageAgreement.tsx` /
   `remote.ts` 注册的 `app` / `back-button` 监听在 Web 下永不触发。桌面浏览器无此键，
   无影响；Android 浏览器里按系统返回键会走浏览器历史导航，不再经过应用内的
  「面板 → 全屏 → 登录框 → 页面栈 → 回首页 → 退出」层级处理。
3. **退出应用**：`plugin:app|exit`（`App.tsx:425`、`UsageAgreement.tsx:73`、
   `TvApp.tsx:41`、`TvPinGate.tsx:293`）在 Web 下没有对应命令；垫片 POST 到
   `/api/tauri` 后由后端返回未知命令错误，调用点均 `.catch(() => {})`，即静默无操作，
   浏览器标签页不会关闭。
4. **iOS 原生播放路径**：`mediaTransport.ts:44` 在 `platform === "ios"` 且用原生引擎时调
   `media_http_origin` 询问 Rust 侧本地媒体服务 origin；Web 下该命令不存在，垫片抛错，
   触发 `.catch` → 回调 `onError("本地媒体服务启动失败，请切换视频或重试")`。hls.js
   路径（`hlsSupported` 为真时）不受影响；iOS 上 MSE 不可用走原生引擎时会看到该报错。
5. **`set_orientation` / `set_text_interaction`**：`Feed.tsx`、`useFeedViewport.ts`、
   `useFsOrientation.ts`、`useInlinePlayerFullscreen.tsx`、`App.tsx` 调用的这两个命令
   是移动端原生能力（锁横竖屏、iOS 文本选择开关）；Web 下后端无此命令，垫片抛错，
   调用点均已 `try/catch` 或 `.catch(() => {})`，静默忽略。浏览器内改用
   `screen.orientation.lock()`（`Feed.tsx` 已有该调用）与元素全屏 API 兜底。
6. **诊断日志落盘**：`playbackDiagnostics.ts` 的 `playback_log_*`（`status` / `set` /
   `write` / `read`）与 `main.tsx` / `api.ts` 的 `debug_log`，Web 下是否落盘取决于
   Python 后端是否实现这几个命令；未实现时垫片抛错，调用点均已 `.catch(() => {})`，
   仅表现为诊断日志不落盘，不影响业务。
7. **桌面窗口控件**：`DesktopChrome.tsx` 的最小化/最大化/关闭按钮与 `ResizeHandles.tsx`
   的边缘拖拽在 Web 下均为 no-op（点击无反应）；`isMaximized` / `isFullscreen` 恒为
   false，标题栏按非最大化渲染。浏览器窗口控制请用浏览器自身按钮。
8. **剪贴板回退的限制**：`execCommand("copy")` 回退要求页面处于用户手势回调内且
   `document` 可写；极端环境下仍可能失败，此时 `writeText` 抛错，调用点按原有
   toast 逻辑提示。

## 5. 验证记录（2026-11-23，标准版）

- 环境：Node v24.22.1、npm 10.9.4；本机无 corepack 二进制（`corepack enable` → `command
  not found`，exit 127），`package.json` 无 `packageManager` 字段，改用
  `npx -y pnpm@10`（实测 pnpm 10.34.9，下载进 npx 缓存，未做全局安装）。
- `npx -y pnpm@10 install --frozen-lockfile` → `Done in 10.2s`，`Packages: +190`，exit `0`。
- `npx -y pnpm@10 build`（即 `tsc --noEmit && tsc --project tsconfig.node.json &&
  tsc --project tsconfig.tests.json && vite build`）→ 三次 `tsc` 均通过，
  `✓ 144 modules transformed`，`✓ built in 6.75s`，exit `0`（一次通过，无需修复）。
  唯一警告：`Some chunks are larger than 500 kB after minification`（`hls-DC5YzZMo.js`
  524.23 kB），为上游既有分包形态，未改动。
- `dist/`：共 26 个文件（`dist/index.html` + `dist/assets/` 内 25 个），总计 1.6M。
  `dist/index.html` 直接引用的两个 asset（`/assets/index-AQIog7_v.js`、
  `/assets/style-BaLEUL-l.css`）均真实存在；对其余 23 个动态 import 的 hashed chunk
  做引用扫描，无缺失（`dynamically referenced hashed chunks missing: NONE`）。
- 静态验证（`python3 -m http.server 4173`，根目录 `dist/`，验证后已停服）：
  `curl http://127.0.0.1:4173/index.html` → `HTTP 200, 9138 bytes`；
  `curl http://127.0.0.1:4173/assets/index-AQIog7_v.js` → `HTTP 200, 231102 bytes`；
  `curl http://127.0.0.1:4173/assets/style-BaLEUL-l.css` → `HTTP 200`。
  主 JS 内含 `window.location.origin`（`oi=window.location.origin`，
  `cover()` 编译为 `` `${oi}/img?u=${encodeUR…` ``），同源化已生效；
  `/api/tauri` 出现 1 次（垫片已进包）。
- `grep -rn 'tauri://\|asset://\|ipc://' dist/` → 无匹配（exit `1`）。
- `grep -ro '17650' dist/` → 0 处（`17650` 已彻底出包；仅存于 `src/shared/api/api.ts:36`
  的解释注释里）。
- `grep -ro 'hg.localhost' dist/` → 2 处，均非媒体基址，是有意保留的检测逻辑，未删除：
  `dist/assets/index-AQIog7_v.js` 1 处（`playbackDiagnostics.ts:87` 的本地媒体 URL
  判定：`url.protocol === "hg:" || url.hostname === "hg.localhost" || …`，只用于诊断
  里识别已包过的本地媒体链接，不构造 URL）；`dist/assets/AcceptedApp-*.js` 1 处
  （`html.ts:26` 的 `isInternalWebviewHost()`，正文链接安全检查，把 `hg.localhost`
  视为内网主机，删掉会削弱检查）。因此「主 JS 不含 `hg.localhost`」的字面 grep
  未全过，但 `LOCAL` 本身已无 `hg.localhost`，不再有任何媒体 URL 由它构造。
  若后续要求产物里一个 `hg.localhost` 字符串都不剩，需另行决定是否动这两处检测逻辑。
