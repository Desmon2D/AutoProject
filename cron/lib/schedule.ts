import { Cron } from 'croner';
import type { ScheduleInput } from './types';

const intervalOptions = [5, 10, 15, 20, 30];

function timeParts(value?:string) {
  const match=/^([01]\d|2[0-3]):([0-5]\d)$/.exec(value??'');
  if(!match) throw new Error('Укажите корректное время');
  return { hour:Number(match[1]), minute:Number(match[2]) };
}

export function scheduleToCron(schedule:ScheduleInput) {
  if(schedule.mode==='interval') {
    const interval=Number(schedule.intervalMinutes);
    if(!intervalOptions.includes(interval)) throw new Error('Некорректный интервал');
    return `*/${interval} * * * *`;
  }
  if(schedule.mode==='hourly') return '0 * * * *';
  if(schedule.mode==='custom') {
    const expression=schedule.cronExpression?.trim()??'';
    if(expression.split(/\s+/).length!==5) throw new Error('Используйте cron из пяти полей');
    return expression;
  }
  const {hour,minute}=timeParts(schedule.time);
  if(schedule.mode==='daily') return `${minute} ${hour} * * *`;
  if(schedule.mode==='weekdays') return `${minute} ${hour} * * 1-5`;
  const days=[...new Set(schedule.weekdays??[])].filter(day=>Number.isInteger(day)&&day>=0&&day<=6).sort();
  if(!days.length) throw new Error('Выберите хотя бы один день недели');
  return `${minute} ${hour} * * ${days.join(',')}`;
}

export function resolveSchedule(schedule:ScheduleInput) {
  try {
    return { cronExpression:scheduleToCron(schedule), error:null };
  } catch(error) {
    return { cronExpression:'', error:error instanceof Error?error.message:'Некорректное расписание' };
  }
}

export function scheduleFromCron(expression:string):ScheduleInput {
  let match=/^\*\/(5|10|15|20|30) \* \* \* \*$/.exec(expression);
  if(match) return {mode:'interval',intervalMinutes:Number(match[1])};
  if(expression==='0 * * * *') return {mode:'hourly'};
  match=/^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5|[0-6](?:,[0-6])*)$/.exec(expression);
  if(match) {
    const time=`${match[2].padStart(2,'0')}:${match[1].padStart(2,'0')}`;
    if(match[3]==='*') return {mode:'daily',time};
    if(match[3]==='1-5') return {mode:'weekdays',time};
    return {mode:'weekly',time,weekdays:match[3].split(',').map(Number)};
  }
  return {mode:'custom',cronExpression:expression};
}

export function describeSchedule(expression:string) {
  const schedule=scheduleFromCron(expression);
  if(schedule.mode==='interval') return `Каждые ${schedule.intervalMinutes} минут`;
  if(schedule.mode==='hourly') return 'Каждый час';
  if(schedule.mode==='daily') return `Ежедневно в ${schedule.time}`;
  if(schedule.mode==='weekdays') return `По будням в ${schedule.time}`;
  if(schedule.mode==='weekly') {
    const labels=['Вс','Пн','Вт','Ср','Чт','Пт','Сб'];
    return `${schedule.weekdays?.map(day=>labels[day]).join(', ')} в ${schedule.time}`;
  }

  const parts=expression.trim().split(/\s+/);
  if(parts.length===5) {
    const [minute,hour,dayOfMonth,month,dayOfWeek]=parts;
    const dayScope=describeDayScope(dayOfWeek);
    const minuteInterval=/^\*\/([1-9]|[1-5]\d)$/.exec(minute);
    if(minuteInterval&&hour==='*'&&dayOfMonth==='*'&&month==='*'&&dayScope!==null) {
      const value=Number(minuteInterval[1]);
      return `${value===1?'Каждую минуту':`Каждые ${value} ${plural(value,'минуту','минуты','минут')}`}${dayScope}`;
    }

    const hourInterval=/^\*\/([1-9]|1\d|2[0-3])$/.exec(hour);
    if(/^\d{1,2}$/.test(minute)&&hourInterval&&dayOfMonth==='*'&&month==='*'&&dayScope!==null) {
      const value=Number(hourInterval[1]);
      const minuteValue=Number(minute);
      if(minuteValue>=0&&minuteValue<=59) {
        const atMinute=minuteValue===0?'':`, на ${String(minuteValue).padStart(2,'0')}-й минуте`;
        return `${value===1?'Каждый час':`Каждые ${value} ${plural(value,'час','часа','часов')}`}${atMinute}${dayScope}`;
      }
    }

    if(/^\d{1,2}$/.test(minute)&&/^\d{1,2}$/.test(hour)&&/^\d{1,2}$/.test(dayOfMonth)&&month==='*'&&dayOfWeek==='*') {
      const time=formatCronTime(hour,minute);
      const day=Number(dayOfMonth);
      if(time&&day>=1&&day<=31) return `Каждого ${day}-го числа в ${time}`;
    }
  }
  return `Cron: ${expression}`;
}

function describeDayScope(value:string) {
  if(value==='*') return '';
  if(value==='1-5') return ' по будням';
  if(value==='0,6'||value==='6,0'||value==='6-7') return ' по выходным';
  if(!/^[0-6](?:,[0-6])*$/.test(value)) return null;
  const labels=['вс','пн','вт','ср','чт','пт','сб'];
  return ` по ${value.split(',').map(item=>labels[Number(item)]).join(', ')}`;
}

function plural(value:number,one:string,few:string,many:string) {
  const mod100=value%100,mod10=value%10;
  if(mod100>=11&&mod100<=14) return many;
  if(mod10===1) return one;
  if(mod10>=2&&mod10<=4) return few;
  return many;
}

function formatCronTime(hour:string,minute:string) {
  const hourValue=Number(hour),minuteValue=Number(minute);
  if(hourValue<0||hourValue>23||minuteValue<0||minuteValue>59) return null;
  return `${String(hourValue).padStart(2,'0')}:${String(minuteValue).padStart(2,'0')}`;
}

export function validateSchedule(expression:string, timezone:string) {
  try { return Boolean(new Cron(expression, { timezone, paused:true }).nextRun()); } catch { return false; }
}
export function getNextRun(expression:string, timezone:string, from = new Date()) {
  return new Cron(expression, { timezone, paused:true }).nextRun(from)?.toISOString() ?? null;
}
export function getNextRuns(expression:string, timezone:string, count=3) {
  return new Cron(expression, { timezone, paused:true }).nextRuns(count).map((date) => date.toISOString());
}
