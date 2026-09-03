export type BashExecutionResult = {
  exitCode:number|null;
  stdout:string;
  stderr:string;
  timedOut:boolean;
};

export type AgentToolEvent = {
  command:string;
  purpose:string;
  result?:BashExecutionResult;
  error?:string;
};

type FunctionCall = {
  type:'function_call';
  call_id:string;
  name:string;
  arguments:string;
};

type ResponseOutput = FunctionCall | {
  type:string;
  content?:Array<{type:string;text?:string}>;
  [key:string]:unknown;
};

type ResponsesApiResult = {
  output?:ResponseOutput[];
  output_text?:string;
  error?:{message?:string};
};

export type AgentConfig = {
  url:string;
  apiKey?:string;
  model:string;
  maxToolCalls?:number;
  fetchImpl?:typeof fetch;
};

export type CodexBridgeConfig = {
  url:string;
  token?:string;
  model:string;
  maxToolCalls?:number;
  fetchImpl?:typeof fetch;
};

type CodexAction = {
  action:'final'|'execute_bash';
  command:string|null;
  purpose:string|null;
  markdown:string|null;
};

const executeBashTool = {
  type:'function',
  name:'execute_bash',
  description:'Выполняет короткую bash-команду в одноразовом изолированном Docker-контейнере. Используй только когда для ответа действительно нужны вычисления или запуск кода.',
  parameters:{
    type:'object',
    properties:{
      command:{type:'string',description:'Команда для bash -lc без интерактивного ввода'},
      purpose:{type:'string',description:'Краткое объяснение, зачем нужен запуск'},
    },
    required:['command','purpose'],
    additionalProperties:false,
  },
  strict:true,
} as const;

function readOutputText(response:ResponsesApiResult) {
  if(response.output_text?.trim()) return response.output_text.trim();
  return (response.output??[])
    .flatMap(item=>item.type==='message'?(item.content??[]):[])
    .filter(part=>part.type==='output_text'&&part.text)
    .map(part=>part.text)
    .join('\n')
    .trim();
}

function parseFunctionArguments(value:string) {
  let parsed:unknown;
  try { parsed=JSON.parse(value); } catch { throw new Error('LLM вернула некорректные аргументы execute_bash'); }
  const input=parsed as {command?:unknown;purpose?:unknown};
  if(typeof input.command!=='string'||!input.command.trim()) throw new Error('LLM не указала bash-команду');
  if(input.command.length>4000) throw new Error('Bash-команда превышает лимит 4000 символов');
  return {command:input.command,purpose:typeof input.purpose==='string'?input.purpose:'Вычисление для задачи'};
}

export async function runResponsesAgent(
  config:AgentConfig,
  userInput:string,
  executeBash:(command:string)=>Promise<BashExecutionResult>,
  onToolEvent?:(event:AgentToolEvent)=>Promise<void>,
  allowBash=true,
) {
  const request=config.fetchImpl??fetch;
  const instructions=`Ты выполняешь автоматическую задачу. Подготовь полезный итоговый отчёт на русском языке в Markdown. Не придумывай факты. ${allowBash?'При необходимости вычислений или запуска небольшого фрагмента кода используй execute_bash. Контейнер не имеет сети и файлов пользователя.':'Инструмент Bash для этой задачи отключён. Не запрашивай выполнение команд.'}`;
  let input:unknown[]=[{role:'user',content:userInput}];
  let toolCalls=0;

  while(true) {
    const response=await request(config.url,{
      method:'POST',
      headers:{'content-type':'application/json',...(config.apiKey?{authorization:`Bearer ${config.apiKey}`}:{})},
      body:JSON.stringify({model:config.model,instructions,input,tools:allowBash?[executeBashTool]:[],tool_choice:allowBash?'auto':'none',parallel_tool_calls:false,max_output_tokens:2400}),
    });
    const raw=await response.text();
    let json:ResponsesApiResult;
    try { json=JSON.parse(raw) as ResponsesApiResult; } catch { throw new Error(`LLM Gateway вернул некорректный JSON (HTTP ${response.status})`); }
    if(!response.ok) throw new Error(json.error?.message??`LLM Gateway вернул HTTP ${response.status}`);

    const calls=(json.output??[]).filter((item):item is FunctionCall=>item.type==='function_call');
    if(!calls.length) {
      const text=readOutputText(json);
      if(!text) throw new Error('LLM Gateway вернул пустой ответ');
      return text;
    }

    const outputs:unknown[]=[];
    for(const call of calls) {
      if(!allowBash) throw new Error('Инструмент Bash отключён для этой задачи');
      if(call.name!=='execute_bash') throw new Error(`LLM запросила неподдерживаемый инструмент: ${call.name}`);
      toolCalls+=1;
      if(toolCalls>(config.maxToolCalls??3)) throw new Error('LLM превысила лимит вызовов execute_bash');
      const {command,purpose}=parseFunctionArguments(call.arguments);
      try {
        const result=await executeBash(command);
        await onToolEvent?.({command,purpose,result});
        outputs.push({type:'function_call_output',call_id:call.call_id,output:JSON.stringify(result)});
      } catch(error) {
        const message=error instanceof Error?error.message:'Неизвестная ошибка code runner';
        await onToolEvent?.({command,purpose,error:message});
        outputs.push({type:'function_call_output',call_id:call.call_id,output:JSON.stringify({error:message})});
      }
    }
    input=[...input,...(json.output??[]),...outputs];
  }
}

async function requestCodexAction(config:CodexBridgeConfig,prompt:string) {
  const request=config.fetchImpl??fetch;
  const response=await request(`${config.url.replace(/\/$/,'')}/llm`,{
    method:'POST',
    headers:{'content-type':'application/json',...(config.token?{authorization:`Bearer ${config.token}`}:{})},
    body:JSON.stringify({model:config.model,prompt}),
  });
  const raw=await response.text();
  let body:{action?:CodexAction;error?:string};
  try { body=JSON.parse(raw) as {action?:CodexAction;error?:string}; } catch { throw new Error(`Codex bridge вернул некорректный ответ (HTTP ${response.status})`); }
  if(!response.ok) throw new Error(body.error??`Codex bridge вернул HTTP ${response.status}`);
  if(!body.action) throw new Error('Codex bridge вернул пустое действие');
  return body.action;
}

export async function runCodexCliAgent(
  config:CodexBridgeConfig,
  userInput:string,
  executeBash:(command:string)=>Promise<BashExecutionResult>,
  onToolEvent?:(event:AgentToolEvent)=>Promise<void>,
  allowBash=true,
) {
  const transcript:string[]=[];
  let toolCalls=0;
  let hasSuccessfulToolResult=false;
  while(true) {
    const prompt=[
      'Ты выполняешь автоматическую задачу. Подготовь итоговый отчёт на русском языке в Markdown и не придумывай факты.',
      allowBash?'Если нужны вычисления или запуск кода, верни действие execute_bash. Это POSIX Bash в Alpine Linux, PowerShell недоступен.':'Инструмент Bash для этой задачи отключён. Обязательно верни действие final и не запрашивай команды.',
      allowBash?'Команда должна быть короткой: только вычислить или проверить данные и вывести сырой результат. Не формируй Markdown-отчёт внутри команды.':'',
      allowBash?'Самостоятельно команды не выполняй и не повторяй уже успешно выполненное вычисление.':'',
      hasSuccessfulToolResult?'Успешный результат инструмента уже получен. Сейчас обязательно верни действие final и используй этот результат в отчёте.':'',
      `Задача и данные:\n${userInput}`,
      transcript.length?`Результаты инструментов:\n${transcript.join('\n\n')}`:'',
    ].filter(Boolean).join('\n\n');
    const action=await requestCodexAction(config,prompt);
    if(action.action==='final') {
      if(!action.markdown?.trim()) throw new Error('Codex bridge вернул пустой итоговый отчёт');
      return action.markdown.trim();
    }
    if(!allowBash) throw new Error('Инструмент Bash отключён для этой задачи');
    if(!action.command?.trim()) throw new Error('Codex bridge не указал bash-команду');
    if(action.command.length>4000) throw new Error('Bash-команда превышает лимит 4000 символов');
    toolCalls+=1;
    if(toolCalls>(config.maxToolCalls??3)) throw new Error('Codex превысил лимит вызовов execute_bash');
    const purpose=action.purpose?.trim()||'Вычисление для задачи';
    try {
      const result=await executeBash(action.command);
      await onToolEvent?.({command:action.command,purpose,result});
      transcript.push(`Команда: ${action.command}\nРезультат: ${JSON.stringify(result)}`);
      hasSuccessfulToolResult=result.exitCode===0&&!result.timedOut&&!result.stderr.trim();
    } catch(error) {
      const message=error instanceof Error?error.message:'Неизвестная ошибка code runner';
      await onToolEvent?.({command:action.command,purpose,error:message});
      transcript.push(`Команда: ${action.command}\nОшибка: ${message}`);
    }
  }
}

export async function executeBashViaRunner(url:string,token:string|undefined,command:string) {
  const response=await fetch(`${url.replace(/\/$/,'')}/execute`,{
    method:'POST',
    headers:{'content-type':'application/json',...(token?{authorization:`Bearer ${token}`}:{})},
    body:JSON.stringify({command}),
  });
  const raw=await response.text();
  let result:(BashExecutionResult&{error?:string});
  try { result=JSON.parse(raw) as BashExecutionResult&{error?:string}; } catch { throw new Error(`Code runner вернул некорректный ответ (HTTP ${response.status})`); }
  if(!response.ok) throw new Error(result.error??`Code runner вернул HTTP ${response.status}`);
  return result;
}
