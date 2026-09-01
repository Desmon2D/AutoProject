import { env } from 'cloudflare:workers';
import { executeBashViaRunner, runCodexCliAgent, runResponsesAgent, type AgentToolEvent } from './llm-agent';
import type { SourceName, TaskRecord } from './types';

export type SourceResult = { source:SourceName; toolName:string; input:unknown; output:unknown };
type RuntimeEnv = {
  MCP_MODE?:string;
  MCP_SERVER_URL?:string;
  LLM_MODE?:string;
  LLM_GATEWAY_URL?:string;
  LLM_API_KEY?:string;
  OPENAI_API_KEY?:string;
  LLM_MODEL?:string;
  CODE_RUNNER_URL?:string;
  CODE_RUNNER_TOKEN?:string;
  CODEX_BRIDGE_URL?:string;
  CODEX_BRIDGE_TOKEN?:string;
};
const runtime=()=>env as unknown as RuntimeEnv;

const demoData:Record<SourceName,unknown>={
  jira:{issues:[{key:'PROJ-142',title:'Ошибка синхронизации прав',priority:'Критичный',status:'В работе'}]},
  git:{changes:[{repository:'auto-project',branch:'main',commit:'a91bc24',summary:'Добавлен безопасный Docker runner',author:'Анна'}]},
  wiki:{pages:[{title:'План релиза 2.4',updated:'сегодня',summary:'Релиз запланирован на пятницу'}]},
  plane:{issues:[{identifier:'CRON-18',title:'Подключить LLM к планировщику',priority:'high',state:'In Progress',assignee:'Михаил'}]},
};

const toolNames:Record<SourceName,string>={
  jira:'jira.get_recent_issues',
  git:'git.get_recent_changes',
  wiki:'wiki.search_relevant_pages',
  plane:'plane.get_recent_issues',
};

export interface McpProvider { call(source:SourceName,task:TaskRecord):Promise<SourceResult>; }

class DemoMcpProvider implements McpProvider {
  async call(source:SourceName,task:TaskRecord) {
    await new Promise(resolve=>setTimeout(resolve,180));
    if(task.prompt.includes(`[fail:${source}]`)) throw new Error(`Демонстрационная ошибка источника ${source}`);
    return {source,toolName:toolNames[source],input:{query:task.name,since:'24h'},output:demoData[source]};
  }
}

class HttpMcpProvider implements McpProvider {
  async call(source:SourceName,task:TaskRecord) {
    const url=runtime().MCP_SERVER_URL;
    if(!url) throw new Error('MCP_SERVER_URL не настроен');
    const input={query:task.name,since:'24h'};
    const response=await fetch(url,{
      method:'POST',
      headers:{'content-type':'application/json','accept':'application/json'},
      body:JSON.stringify({jsonrpc:'2.0',id:crypto.randomUUID(),method:'tools/call',params:{name:toolNames[source],arguments:input}}),
    });
    if(!response.ok) throw new Error(`MCP вернул HTTP ${response.status}`);
    const json=await response.json() as {result?:unknown;error?:{message?:string}};
    if(json.error) throw new Error(json.error.message??'Ошибка MCP');
    return {source,toolName:toolNames[source],input,output:json.result};
  }
}

export function getMcpProvider():McpProvider {
  return runtime().MCP_MODE==='real'?new HttpMcpProvider():new DemoMcpProvider();
}

function demoReport(task:TaskRecord,results:SourceResult[],failures:string[]) {
  const sections=results.map(result=>`## ${result.source}\n\n\`\`\`json\n${JSON.stringify(result.output,null,2)}\n\`\`\``).join('\n\n');
  return `# Сводка: ${task.name}\n\n${sections||'Источники не выбраны.'}${failures.length?`\n\n> Предупреждение: ${failures.join('; ')}`:''}`;
}

export async function generateReport(
  task:TaskRecord,
  results:SourceResult[],
  failures:string[],
  onToolEvent?:(event:AgentToolEvent)=>Promise<void>,
) {
  const config=runtime();
  if(!config.LLM_MODE||config.LLM_MODE==='demo') return demoReport(task,results,failures);

  const model=config.LLM_MODEL||(config.LLM_MODE==='codex'?'gpt-5.6-luna':'gpt-5');
  const runnerUrl=config.CODE_RUNNER_URL||'http://127.0.0.1:3010';
  const context=[
    `Название задачи: ${task.name}`,
    `Инструкция пользователя:\n${task.prompt}`,
    `Полученные данные:\n${JSON.stringify(results,null,2)}`,
    failures.length?`Ошибки источников:\n${failures.join('\n')}`:'',
  ].filter(Boolean).join('\n\n');
  const executeBash=(command:string)=>executeBashViaRunner(runnerUrl,config.CODE_RUNNER_TOKEN,command);
  const allowBash=task.tools.includes('bash');

  if(config.LLM_MODE==='codex') {
    return runCodexCliAgent(
      {url:config.CODEX_BRIDGE_URL||runnerUrl,token:config.CODEX_BRIDGE_TOKEN||config.CODE_RUNNER_TOKEN,model},
      context,
      executeBash,
      onToolEvent,
      allowBash,
    );
  }
  if(config.LLM_MODE!=='real') throw new Error(`Неизвестный LLM_MODE: ${config.LLM_MODE}`);
  return runResponsesAgent(
    {url:config.LLM_GATEWAY_URL||'https://api.openai.com/v1/responses',apiKey:config.LLM_API_KEY||config.OPENAI_API_KEY,model},
    context,
    executeBash,
    onToolEvent,
    allowBash,
  );
}
