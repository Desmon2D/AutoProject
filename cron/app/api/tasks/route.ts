import { createTask, deleteTask, listTasks, updateTask } from '../../../lib/database';
import { getNextRun, getNextRuns, scheduleToCron, validateSchedule } from '../../../lib/schedule';
import type { ScheduleInput, SourceName, TaskInput } from '../../../lib/types';

function parseInput(value:unknown):TaskInput {
  const v=value as Partial<TaskInput> & { schedule?:ScheduleInput };
  const sources=(v.sources??[]).filter((s):s is SourceName=>['jira','git','wiki'].includes(s));
  const cronExpression=v.schedule?scheduleToCron(v.schedule):v.cronExpression;
  if(!v.name?.trim()||!v.prompt?.trim()||!cronExpression||!v.timezone||!sources.length) throw new Error('Заполните название, инструкцию, расписание и источники');
  if(!validateSchedule(cronExpression,v.timezone)) throw new Error('Некорректное расписание или часовой пояс');
  return {name:v.name.trim(),description:v.description?.trim()??'',prompt:v.prompt.trim(),cronExpression,timezone:v.timezone,enabled:v.enabled!==false,sources};
}

export async function GET(request:Request){ const url=new URL(request.url); const expression=url.searchParams.get('expression'),timezone=url.searchParams.get('timezone'); if(expression&&timezone){ if(!validateSchedule(expression,timezone)) return Response.json({error:'Некорректное расписание'},{status:400}); return Response.json({nextRuns:getNextRuns(expression,timezone)}); } return Response.json({tasks:await listTasks()}); }
export async function POST(request:Request){ try { const input=parseInput(await request.json()); return Response.json({task:await createTask(input,input.enabled?getNextRun(input.cronExpression,input.timezone):null)},{status:201}); } catch(error){ return Response.json({error:error instanceof Error?error.message:'Ошибка'},{status:400}); } }
export async function PATCH(request:Request){ try { const url=new URL(request.url),id=url.searchParams.get('id'); if(!id) throw new Error('Не указан id'); const input=parseInput(await request.json()); return Response.json({task:await updateTask(id,input,input.enabled?getNextRun(input.cronExpression,input.timezone):null)}); } catch(error){ return Response.json({error:error instanceof Error?error.message:'Ошибка'},{status:400}); } }
export async function DELETE(request:Request){ const id=new URL(request.url).searchParams.get('id'); if(!id) return Response.json({error:'Не указан id'},{status:400}); await deleteTask(id); return Response.json({ok:true}); }
