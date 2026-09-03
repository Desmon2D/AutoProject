import { describe, expect, it, vi } from 'vitest';
import { runCodexCliAgent, runResponsesAgent } from './llm-agent';

describe('Responses API agent',()=>{
  it('executes a requested bash tool and returns the final report',async()=>{
    const request=vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({output:[{type:'function_call',call_id:'call_1',name:'execute_bash',arguments:JSON.stringify({command:'printf 42',purpose:'Посчитать ответ'})}]})))
      .mockResolvedValueOnce(new Response(JSON.stringify({output:[{type:'message',content:[{type:'output_text',text:'# Результат\n\nОтвет: 42'}]}]})));
    const execute=vi.fn().mockResolvedValue({exitCode:0,stdout:'42',stderr:'',timedOut:false});
    const observer=vi.fn();

    const report=await runResponsesAgent({url:'https://example.test/v1/responses',model:'test',fetchImpl:request},'Посчитай',execute,observer);

    expect(report).toContain('Ответ: 42');
    expect(execute).toHaveBeenCalledWith('printf 42');
    expect(observer).toHaveBeenCalledWith(expect.objectContaining({command:'printf 42',result:expect.objectContaining({stdout:'42'})}));
    const secondBody=JSON.parse(request.mock.calls[1][1]?.body as string);
    expect(secondBody.input).toContainEqual(expect.objectContaining({type:'function_call_output',call_id:'call_1'}));
  });

  it('rejects more tool calls than allowed',async()=>{
    const request=vi.fn().mockImplementation(()=>Promise.resolve(new Response(JSON.stringify({output:[{type:'function_call',call_id:'call_1',name:'execute_bash',arguments:'{"command":"true","purpose":"test"}'}]}))));
    await expect(runResponsesAgent({url:'https://example.test',model:'test',maxToolCalls:1,fetchImpl:request},'test',async()=>({exitCode:0,stdout:'',stderr:'',timedOut:false}))).rejects.toThrow('лимит');
  });

  it('does not expose execute_bash when the task disables it',async()=>{
    const request=vi.fn().mockResolvedValue(new Response(JSON.stringify({output:[{type:'message',content:[{type:'output_text',text:'Готово'}]}]})));
    await runResponsesAgent({url:'https://example.test',model:'test',fetchImpl:request},'test',async()=>({exitCode:0,stdout:'',stderr:'',timedOut:false}),undefined,false);
    const body=JSON.parse(request.mock.calls[0][1]?.body as string);
    expect(body.tools).toEqual([]);
    expect(body.tool_choice).toBe('none');
  });
});

describe('Codex CLI agent',()=>{
  it('routes a structured bash action through the Docker runner',async()=>{
    const request=vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({action:{action:'execute_bash',command:'printf 42',purpose:'Посчитать',markdown:null}})))
      .mockResolvedValueOnce(new Response(JSON.stringify({action:{action:'final',command:null,purpose:null,markdown:'# Ответ\n\n42'}})));
    const execute=vi.fn().mockResolvedValue({exitCode:0,stdout:'42',stderr:'',timedOut:false});

    const report=await runCodexCliAgent({url:'http://127.0.0.1:3010',model:'gpt-5.6-luna',fetchImpl:request},'Посчитай',execute);

    expect(report).toContain('42');
    expect(execute).toHaveBeenCalledWith('printf 42');
    const secondPrompt=JSON.parse(request.mock.calls[1][1]?.body as string).prompt;
    expect(secondPrompt).toContain('"stdout":"42"');
  });

  it('rejects a bash action when the task disables it',async()=>{
    const request=vi.fn().mockResolvedValue(new Response(JSON.stringify({action:{action:'execute_bash',command:'true',purpose:'test',markdown:null}})));
    const execute=vi.fn();
    await expect(runCodexCliAgent({url:'http://127.0.0.1:3010',model:'gpt-5.6-luna',fetchImpl:request},'test',execute,undefined,false)).rejects.toThrow('отключён');
    expect(execute).not.toHaveBeenCalled();
  });
});
