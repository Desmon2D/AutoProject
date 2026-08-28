import { env } from 'cloudflare:workers';
import type { SourceName, TaskRecord } from './types';

export type SourceResult = { source:SourceName; toolName:string; input:unknown; output:unknown };
type RuntimeEnv = { MCP_MODE?:string; MCP_SERVER_URL?:string; LLM_MODE?:string; LLM_GATEWAY_URL?:string; LLM_API_KEY?:string; LLM_MODEL?:string };
const runtime = () => env as unknown as RuntimeEnv;

const demoData:Record<SourceName,unknown> = {
  jira: { issues:[{key:'PROJ-142',title:'Ошибка синхронизации прав',priority:'Критичный',status:'В работе'},{key:'PROJ-138',title:'Ускорить загрузку отчёта',priority:'Высокий',status:'На ревью'}] },
  git: { changes:[{repository:'project-api',branch:'main',summary:'Добавлена повторная обработка запросов',author:'Анна'},{repository:'project-web',branch:'main',summary:'Обновлена страница истории запусков',author:'Михаил'}] },
  wiki: { pages:[{title:'План релиза 2.4',updated:'сегодня',summary:'Релиз запланирован на пятницу'},{title:'Runbook: синхронизация',updated:'вчера',summary:'Добавлены шаги восстановления'}] },
};

const toolNames:Record<SourceName,string> = { jira:'jira.get_recent_issues', git:'git.get_recent_changes', wiki:'wiki.search_relevant_pages' };

export interface McpProvider { call(source:SourceName, task:TaskRecord):Promise<SourceResult>; }

class DemoMcpProvider implements McpProvider {
  async call(source:SourceName, task:TaskRecord) {
    await new Promise((resolve) => setTimeout(resolve, 180));
    if (task.prompt.includes(`[fail:${source}]`)) throw new Error(`Демонстрационная ошибка источника ${source}`);
    return { source, toolName:toolNames[source], input:{ query:task.name, since:'24h' }, output:demoData[source] };
  }
}

class HttpMcpProvider implements McpProvider {
  async call(source:SourceName, task:TaskRecord) {
    const url=runtime().MCP_SERVER_URL;
    if(!url) throw new Error('MCP_SERVER_URL не настроен');
    const input={ query:task.name, since:'24h' };
    const response=await fetch(url,{method:'POST',headers:{'content-type':'application/json','accept':'application/json, text/event-stream'},body:JSON.stringify({jsonrpc:'2.0',id:crypto.randomUUID(),method:'tools/call',params:{name:toolNames[source],arguments:input}})});
    if(!response.ok) throw new Error(`MCP вернул HTTP ${response.status}`);
    const json=await response.json() as {result?:unknown;error?:{message?:string}};
    if(json.error) throw new Error(json.error.message??'Ошибка MCP');
    return {source,toolName:toolNames[source],input,output:json.result};
  }
}

export function getMcpProvider():McpProvider { return runtime().MCP_MODE==='real'?new HttpMcpProvider():new DemoMcpProvider(); }

function demoReport(task:TaskRecord, results:SourceResult[], failures:string[]) {
  const has=(source:SourceName)=>results.find((item)=>item.source===source)?.output as Record<string,unknown>|undefined;
  const jira=has('jira')?.issues as Array<Record<string,string>>|undefined;
  const git=has('git')?.changes as Array<Record<string,string>>|undefined;
  const wiki=has('wiki')?.pages as Array<Record<string,string>>|undefined;
  return `# Сводка: ${task.name}\n\n## Критичные задачи\n${jira?.map((i)=>`- **${i.key}** — ${i.title} (${i.priority}, ${i.status})`).join('\n')??'- Данные Jira недоступны'}\n\n## Изменения в коде\n${git?.map((i)=>`- **${i.repository}**: ${i.summary} — ${i.author}`).join('\n')??'- Данные Git недоступны'}\n\n## Документация\n${wiki?.map((i)=>`- **${i.title}** — ${i.summary}`).join('\n')??'- Данные Wiki недоступны'}\n\n## Рекомендуемые действия\n1. Проверить прогресс по критичной задаче PROJ-142.\n2. Завершить ревью изменений перед релизом.\n3. Сверить план релиза с обновлённым runbook.${failures.length?`\n\n> Предупреждение: ${failures.join('; ')}`:''}`;
}

export async function generateReport(task:TaskRecord,results:SourceResult[],failures:string[]) {
  if(runtime().LLM_MODE!=='real') return demoReport(task,results,failures);
  const {LLM_GATEWAY_URL:url,LLM_API_KEY:key,LLM_MODEL:model}=runtime();
  if(!url) throw new Error('LLM_GATEWAY_URL не настроен');
  const response=await fetch(url,{method:'POST',headers:{'content-type':'application/json',...(key?{authorization:`Bearer ${key}`}:{})},body:JSON.stringify({model:model??'default',messages:[{role:'system',content:'Сформируй итоговый отчёт в Markdown.'},{role:'user',content:`${task.prompt}\n\nДанные:\n${JSON.stringify(results)}`} ]})});
  if(!response.ok) throw new Error(`LLM Gateway вернул HTTP ${response.status}`);
  const json=await response.json() as {output_text?:string;choices?:Array<{message?:{content?:string}}>};
  const text=json.output_text??json.choices?.[0]?.message?.content;
  if(!text) throw new Error('LLM Gateway вернул пустой ответ');
  return text;
}
