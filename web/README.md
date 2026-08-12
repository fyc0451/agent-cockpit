# Cockpit 2.0 Web Shell（W1）

React + TypeScript + Vite 的正式 App Shell。技术栈（development-readiness-plan §4 冻结）：
Hash 路由（react-router-dom）、TanStack Query、手写 typed client、CSS semantic tokens（无组件库）、
轻量 Context 管理 UI state，仅设备偏好写 localStorage。

## 命令

```bash
npm --prefix web install
npm --prefix web run dev      # vite dev，/api 代理到 http://127.0.0.1:18790（生产用相对路径）
npm --prefix web run build    # tsc --noEmit && vite build → web/dist（base: './'）
npm --prefix web test         # vitest（jsdom + testing-library）
npm --prefix web run test:e2e # playwright + axe（vite preview 起静态服务，/api 全部 page.route 拦截，不接真实后端）
```

## 目录

```
web/
├── app/         # 入口 main.tsx、路由表 App.tsx、routes.ts（路由单一权威：patterns/builders/nav 元数据）
├── shell/       # AppShell：Rail / TopBar / ProjectDrawer / WorkspaceSwitcher / CommandPalette
├── pages/       # 全部冻结路由的页面骨架
├── components/  # StatusState（G6 状态族）、Tag、Button、PageHeader、useDialog
├── features/    # ProjectScope / WorkspaceScope（深链校验 + selection 写入）、nav
├── api/         # typed client（client/types/hooks/normalize/errorState）
├── state/       # capabilities registry、selection Context、theme
├── fixtures/    # 测试用后端载荷
├── styles/      # global.css（semantic tokens，亮/暗双主题）
└── test/        # vitest 测试
```

## Capability registry（红线）

`state/capabilities.tsx` 是能力权威合并层：静态 registry 全部 fail-closed
（`available=false` + 真实 reason），任何 query 返回的 `meta.capabilities` 经
`useReportCapabilities(meta, scope)` 按 scope key（`global` / `p:<slug>` / `w:<slug>/<wid>`，
从 query key 取）推入 `CapabilitiesProvider`；同 scope snapshot 为 replace 语义，
离开 scope 即失效（无跨 project 泄漏）。读取顺序为当前 scope 的 server 值 →
静态 fallback → 未声明 fail-closed。React 组件用 `useCapability(key, scope)` 读，
不得按路径/颜色猜。`available=false` 时整页或区块渲染 forbidden/unavailable 状态 +
真实原因 + 文档入口；写按钮一律 `aria-disabled` + `aria-describedby` reason
（可聚焦、激活无效）。W1 没有真写操作，全部禁用。

## 深链合同（G1）

Hash 路由，path segment 放 ID，query 放筛选/子视图：

```
#/overview  #/welcome  #/projects  #/inbox?view=needs-action  #/settings?view=doctor
#/projects/:slug/{workbench,memory,recovery,activity}
#/projects/:slug/workspaces/:wid[/{activity,files,terminal,tasks,git,editor,browser}]
```

刷新/复制链接/前进后退恢复同一 Project、Workspace、页面和子视图；进入 URL 时
ProjectScope/WorkspaceScope 通过 typed client 校验存在性，不存在给 error/empty 态，
存在则写入 selection Context（`state/selection.tsx`）。未知路由重定向 `#/overview`。
所有 `<Route path>` pattern 与链接构造集中在 `app/routes.ts`（`PROJECT_PARAM`/`WORKSPACE_PARAM`
是 URL 身份单点，slug vs project_id 裁决未定，落地后只改该文件）；业务代码不得写字面量路径。

## 错误模型（G3）

`api/client.ts` 解析 `{ error: { code, message, retryable, request_id, details } }` envelope 为
`ApiError`；`{ data, meta }` envelope 的 `meta.partial` / `meta.sources` 透出给页面渲染
degraded 态；网络失败映射 disconnected。query error 按 `ApiError.code`/`status` 映射到
G6 状态组件（`api/errorState.ts`）。

## 主题

`localStorage['cockpit-v2-theme']` ∈ `system|light|dark`，解析结果写 `<html data-theme>`；
`index.html` 内联脚本防 FOUC。rail 与终端区域在两种主题下都保持深色。
