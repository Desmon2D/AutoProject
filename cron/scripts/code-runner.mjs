import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { loadEnvFile } from 'node:process';

try { loadEnvFile('.env'); } catch { /* .env is optional */ }

const host=process.env.CODE_RUNNER_HOST||'127.0.0.1';
const port=Number(process.env.CODE_RUNNER_PORT||3010);
const image=process.env.CODE_RUNNER_IMAGE||'bash:5.2-alpine3.22';
const token=process.env.CODE_RUNNER_TOKEN||'';
const bashTimeoutMs=Number(process.env.CODE_RUNNER_TIMEOUT_MS||10_000);
const codexTimeoutMs=Number(process.env.CODEX_TIMEOUT_MS||120_000);
const maxOutputBytes=Number(process.env.CODE_RUNNER_MAX_OUTPUT_BYTES||65_536);
const dockerBin=process.env.DOCKER_BIN||(
  process.platform==='win32'&&process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA,'Programs','DockerDesktop','resources','bin','docker.exe')
    : 'docker'
);
const codexRoot=process.env.CODEX_NODE_ROOT||(
  process.platform==='win32'&&process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA,'hermes','node')
    : path.dirname(process.execPath)
);
const codexNode=process.env.CODEX_NODE_BIN||(
  process.platform==='win32'?path.join(codexRoot,'node.exe'):process.execPath
);
const codexCliJs=process.env.CODEX_CLI_JS||path.join(codexRoot,'node_modules','@openai','codex','bin','codex.js');

const actionSchema={
  type:'object',
  properties:{
    action:{type:'string',enum:['final','execute_bash']},
    command:{type:['string','null']},
    purpose:{type:['string','null']},
    markdown:{type:['string','null']},
  },
  required:['action','command','purpose','markdown'],
  additionalProperties:false,
};

function json(response,status,payload) {
  response.writeHead(status,{'content-type':'application/json; charset=utf-8'});
  response.end(JSON.stringify(payload));
}

function readJson(request,maxBytes=524_288) {
  return new Promise((resolve,reject)=>{
    let body='';
    let rejected=false;
    request.setEncoding('utf8');
    request.on('data',chunk=>{
      if(rejected) return;
      body+=chunk;
      if(Buffer.byteLength(body)>maxBytes) {
        rejected=true;
        reject(new Error('Тело запроса превышает лимит'));
      }
    });
    request.on('end',()=>{
      if(rejected) return;
      try { resolve(JSON.parse(body||'{}')); } catch { reject(new Error('Некорректный JSON')); }
    });
    request.on('error',reject);
  });
}

function cleanupContainer(name) {
  const cleanup=spawn(dockerBin,['rm','-f',name],{stdio:'ignore',windowsHide:true});
  cleanup.unref();
}

function executeBash(command) {
  return new Promise((resolve,reject)=>{
    const containerName=`ai-cron-${randomUUID()}`;
    const args=[
      'run','--rm','--name',containerName,
      '--network','none','--read-only','--cap-drop','ALL',
      '--security-opt','no-new-privileges','--pids-limit','64',
      '--memory','128m','--cpus','0.5',
      '--tmpfs','/tmp:rw,nosuid,nodev,size=16m','--user','65534:65534',
      image,'bash','-lc',command,
    ];
    const child=spawn(dockerBin,args,{stdio:['ignore','pipe','pipe'],windowsHide:true});
    let stdout='';
    let stderr='';
    let outputBytes=0;
    let settled=false;
    let timer;
    const finish=(error,result)=>{
      if(settled) return;
      settled=true;
      if(timer) clearTimeout(timer);
      if(error) reject(error); else resolve(result);
    };
    const append=(target,chunk)=>{
      outputBytes+=chunk.length;
      if(outputBytes>maxOutputBytes) {
        child.kill();
        cleanupContainer(containerName);
        finish(new Error(`Вывод команды превышает лимит ${maxOutputBytes} байт`));
        return target;
      }
      return target+chunk.toString('utf8');
    };
    child.stdout.on('data',chunk=>{ stdout=append(stdout,chunk); });
    child.stderr.on('data',chunk=>{ stderr=append(stderr,chunk); });
    child.on('error',error=>finish(new Error(`Не удалось запустить Docker: ${error.message}`)));
    child.on('close',code=>finish(null,{exitCode:code,stdout,stderr,timedOut:false}));
    timer=setTimeout(()=>{
      child.kill();
      cleanupContainer(containerName);
      finish(null,{exitCode:null,stdout,stderr,timedOut:true});
    },bashTimeoutMs);
  });
}

function runCodexProcess(args,input) {
  return new Promise((resolve,reject)=>{
    const child=spawn(codexNode,[codexCliJs,...args],{stdio:['pipe','pipe','pipe'],windowsHide:true});
    let stdout='';
    let stderr='';
    let outputBytes=0;
    let settled=false;
    let timer;
    const finish=(error,result)=>{
      if(settled) return;
      settled=true;
      if(timer) clearTimeout(timer);
      if(error) reject(error); else resolve(result);
    };
    const append=(target,chunk)=>{
      outputBytes+=chunk.length;
      if(outputBytes>1_048_576) {
        child.kill();
        finish(new Error('Вывод Codex CLI превышает лимит'));
        return target;
      }
      return target+chunk.toString('utf8');
    };
    child.stdout.on('data',chunk=>{ stdout=append(stdout,chunk); });
    child.stderr.on('data',chunk=>{ stderr=append(stderr,chunk); });
    child.on('error',error=>finish(new Error(`Не удалось запустить Codex CLI: ${error.message}`)));
    child.on('close',code=>code===0?finish(null,{stdout,stderr}):finish(new Error(stderr.trim()||stdout.trim()||`Codex CLI завершился с кодом ${code}`)));
    timer=setTimeout(()=>{
      child.kill();
      finish(new Error(`Codex CLI превысил лимит времени ${codexTimeoutMs} мс`));
    },codexTimeoutMs);
    child.stdin.end(input,'utf8');
  });
}

async function executeCodex(prompt,model) {
  if(!existsSync(codexNode)||!existsSync(codexCliJs)) throw new Error('Codex CLI не найден на этом устройстве');
  const callDir=await mkdtemp(path.join(os.tmpdir(),'ai-cron-codex-'));
  const schemaPath=path.join(callDir,'action-schema.json');
  const outputPath=path.join(callDir,'result.json');
  try {
    await writeFile(schemaPath,JSON.stringify(actionSchema),'utf8');
    const args=[
      'exec','--model',model,'--sandbox','read-only','--ephemeral',
      '--ignore-user-config','--ignore-rules','--skip-git-repo-check','--color','never',
      '--disable','shell_tool','--disable','code_mode_host','--disable','apps',
      '--disable','browser_use','--disable','computer_use','--disable','image_generation',
      '--output-schema',schemaPath,'--output-last-message',outputPath,'-C',callDir,'-',
    ];
    await runCodexProcess(args,prompt);
    const action=JSON.parse(await readFile(outputPath,'utf8'));
    if(!['final','execute_bash'].includes(action.action)) throw new Error('Codex CLI вернул неизвестное действие');
    return action;
  } finally {
    await rm(callDir,{recursive:true,force:true});
  }
}

const server=http.createServer(async(request,response)=>{
  if(request.method==='GET'&&request.url==='/health') return json(response,200,{ok:true,image,codex:existsSync(codexCliJs)});
  if(request.method!=='POST'||!['/execute','/llm'].includes(request.url||'')) return json(response,404,{error:'Маршрут не найден'});
  if(token&&request.headers.authorization!==`Bearer ${token}`) return json(response,401,{error:'Неверный CODE_RUNNER_TOKEN'});
  try {
    const body=await readJson(request);
    if(request.url==='/llm') {
      if(typeof body.prompt!=='string'||!body.prompt.trim()) return json(response,400,{error:'Поле prompt обязательно'});
      if(typeof body.model!=='string'||!body.model.trim()) return json(response,400,{error:'Поле model обязательно'});
      return json(response,200,{action:await executeCodex(body.prompt,body.model)});
    }
    if(typeof body.command!=='string'||!body.command.trim()) return json(response,400,{error:'Поле command обязательно'});
    if(body.command.length>4000) return json(response,400,{error:'Команда превышает лимит 4000 символов'});
    return json(response,200,await executeBash(body.command));
  } catch(error) {
    return json(response,500,{error:error instanceof Error?error.message:'Ошибка локального runtime'});
  }
});

server.listen(port,host,()=>console.log(`[local-runtime] http://${host}:${port}; image=${image}; codex=${existsSync(codexCliJs)}`));
