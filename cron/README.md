# AI Cron MVP

Локальный прототип AI-задач, выполняемых вручную и по cron-расписанию.

## Запуск

Требуется Node.js 22.13 или новее.

```bash
npm install
npm run dev:all
```

Откройте `http://127.0.0.1:3000`.

При первом обращении создаётся демонстрационная задача. Данные и история сохраняются в локальной D1/SQLite базе проекта.

## Режимы интеграций

По умолчанию используются предсказуемые demo MCP и demo LLM. Скопируйте `.env.example` в `.env` и задайте переменные для реальных подключений:

```env
MCP_MODE=real
MCP_SERVER_URL=https://mcp.example.com
LLM_MODE=real
LLM_GATEWAY_URL=https://llm.example.com/chat/completions
LLM_API_KEY=
LLM_MODEL=
```

## Проверка

```bash
npm test
npm run lint
npm run build
```
