import { listDueTasks, setTaskNextRun } from '../../../lib/database';
import { executeTask } from '../../../lib/execution';
import { getNextRun } from '../../../lib/schedule';

export async function POST(){ const now=new Date(); const tasks=await listDueTasks(now.toISOString()); const runIds:string[]=[]; for(const task of tasks){ await setTaskNextRun(task.id,getNextRun(task.cronExpression,task.timezone,new Date(now.getTime()+1000))); runIds.push(await executeTask(task.id,'scheduled')); } return Response.json({processed:runIds.length,runIds}); }
