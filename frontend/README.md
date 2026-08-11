# PGAgent 前端

PGAgent 的本地 Web 工作台，使用 React、Vite 和 TypeScript。界面包含总览、工作区、Agent、会话、运行记录、团队任务和模型设置七个视图。

## 本地开发

```powershell
npm install
npm run dev
```

开发服务器默认监听 `http://127.0.0.1:5173`，并将 `/api` 转发到 `http://127.0.0.1:8000`。如需连接其他后端，可设置 `VITE_API_BASE_URL`。

## 校验

```powershell
npm run build
npm run lint
npm test
```

前端不会用演示数据伪装成功：后端不可连接或返回错误时，页面会展示明确错误和重试操作。
