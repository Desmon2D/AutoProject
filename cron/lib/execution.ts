import { addToolCall, createRun, getTask, hasActiveRun, updateRun } from './database';
import { generateReport, getMcpProvider, type SourceResult } from './integrations';
import type { RunRecord } from './types';

export async function executeTask(taskId:string,triggerType:RunRecord['triggerType']='manual',retryOfRunId:string|null=null) {
  const task=await getTask(taskId);
  if(!task) throw new Error('Задача не найдена');
  if(await hasActiveRun(taskId)) {
    if(triggerType!=='scheduled') throw new Error('Задача уже выполняется');
    const skippedId=await createRun(taskId,triggerType,retryOfRunId);
    await updateRun(skippedId,{status:'skipped',finishedAt:new Date().toISOString(),warning:'Предыдущий запуск ещё выполняется'});
    return skippedId;
  }
  const runId=await createRun(taskId,triggerType,retryOfRunId);
  await updateRun(runId,{status:'running',startedAt:new Date().toISOString()});
  const provider=getMcpProvider();
  const results:SourceResult[]=[];
  const failures:string[]=[];
  await Promise.all(task.sources.map(async(source)=>{
    const startedAt=new Date().toISOString();
    try {
      const value=await provider.call(source,task); results.push(value);
      await addToolCall({runId,source,toolName:value.toolName,status:'success',inputJson:JSON.stringify(value.input),outputJson:JSON.stringify(value.output),errorMessage:null,startedAt,finishedAt:new Date().toISOString()});
    } catch(error) {
      const message=error instanceof Error?error.message:'Неизвестная ошибка'; failures.push(`${source}: ${message}`);
      await addToolCall({runId,source,toolName:`${source}.unknown`,status:'failed',inputJson:'{}',outputJson:null,errorMessage:message,startedAt,finishedAt:new Date().toISOString()});
    }
  }));
  try {
    if(!results.length) throw new Error('Все источники завершились с ошибкой');
    const report=await generateReport(task,results,failures);
    await updateRun(runId,{status:'success',finishedAt:new Date().toISOString(),result:report,warning:failures.length?failures.join('; '):undefined});
  } catch(error) {
    await updateRun(runId,{status:'failed',finishedAt:new Date().toISOString(),error:error instanceof Error?error.message:'Неизвестная ошибка',warning:failures.join('; ')||undefined});
  }
  return runId;
}
