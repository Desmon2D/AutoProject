import { listRuns } from '../../../lib/database';
import { executeTask } from '../../../lib/execution';

export async function GET(request:Request){ return Response.json({runs:await listRuns(new URL(request.url).searchParams.get('taskId')??undefined)}); }
export async function POST(request:Request){ try { const body=await request.json() as {taskId?:string;retryOfRunId?:string}; if(!body.taskId) throw new Error('Не указана задача'); const runId=await executeTask(body.taskId,body.retryOfRunId?'retry':'manual',body.retryOfRunId??null); return Response.json({runId}); } catch(error){ return Response.json({error:error instanceof Error?error.message:'Ошибка запуска'},{status:400}); } }
