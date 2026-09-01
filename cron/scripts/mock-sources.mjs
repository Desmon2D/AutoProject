import http from 'node:http';
import { loadEnvFile } from 'node:process';

try { loadEnvFile('.env'); } catch { /* .env is optional */ }

const host=process.env.MOCK_SOURCES_HOST||'127.0.0.1';
const port=Number(process.env.MOCK_SOURCES_PORT||3020);

const gitResult={
  repository:'Desmon2D/AutoProject',
  defaultBranch:'add-cron-project',
  changes:[
    {commit:'8f27d1a',author:'Alex',summary:'Добавлен запуск задач по человекочитаемому расписанию',filesChanged:4,additions:146,deletions:21},
    {commit:'31ac906',author:'Maria',summary:'Исправлена проверка выбранных дней недели',filesChanged:2,additions:38,deletions:12},
    {commit:'c04e112',author:'Ivan',summary:'Обновлена страница истории запусков',filesChanged:3,additions:91,deletions:34},
  ],
};

const planeResult={
  workspace:'AutoProject',
  project:'AI Cron MVP',
  issues:[
    {identifier:'CRON-18',title:'Подключить реальную LLM',priority:'high',state:'In Progress',assignee:'Ivan'},
    {identifier:'CRON-21',title:'Добавить изолированный code runner',priority:'high',state:'In Review',assignee:'Maria'},
    {identifier:'CRON-24',title:'Подготовить mock-источники Git и Plane',priority:'medium',state:'Done',assignee:'Alex'},
  ],
};

function json(response,status,payload) {
  response.writeHead(status,{'content-type':'application/json; charset=utf-8'});
  response.end(JSON.stringify(payload));
}

function readJson(request) {
  return new Promise((resolve,reject)=>{
    let body='';
    request.setEncoding('utf8');
    request.on('data',chunk=>{ body+=chunk; });
    request.on('end',()=>{
      try { resolve(JSON.parse(body||'{}')); } catch { reject(new Error('Некорректный JSON')); }
    });
    request.on('error',reject);
  });
}

const server=http.createServer(async(request,response)=>{
  if(request.method==='GET'&&request.url==='/health') return json(response,200,{ok:true,sources:['git','plane']});
  if(request.method!=='POST'||request.url!=='/mcp') return json(response,404,{error:'Маршрут не найден'});
  try {
    const rpc=await readJson(request);
    if(rpc.method==='tools/list') return json(response,200,{jsonrpc:'2.0',id:rpc.id,result:{tools:[
      {name:'git.get_recent_changes',description:'Последние изменения тестового Git-репозитория'},
      {name:'plane.get_recent_issues',description:'Актуальные задачи тестового проекта Plane'},
    ]}});
    if(rpc.method!=='tools/call') return json(response,200,{jsonrpc:'2.0',id:rpc.id,error:{code:-32601,message:'Метод не найден'}});
    const tool=rpc.params?.name;
    const result=tool==='git.get_recent_changes'?gitResult:tool==='plane.get_recent_issues'?planeResult:null;
    if(!result) return json(response,200,{jsonrpc:'2.0',id:rpc.id,error:{code:-32602,message:`Неизвестный mock-инструмент: ${tool}`}});
    await new Promise(resolve=>setTimeout(resolve,120));
    return json(response,200,{jsonrpc:'2.0',id:rpc.id,result});
  } catch(error) {
    return json(response,400,{error:error instanceof Error?error.message:'Ошибка mock-сервера'});
  }
});

server.listen(port,host,()=>console.log(`[mock-sources] http://${host}:${port}/mcp; git, plane`));
