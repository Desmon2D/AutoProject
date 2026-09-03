import { describe, expect, it } from 'vitest';
import { describeSchedule, getNextRun, getNextRuns, resolveSchedule, scheduleFromCron, scheduleToCron, validateSchedule } from './schedule';

describe('schedule', () => {
  it('validates cron and timezone', () => {
    expect(validateSchedule('0 9 * * 1-5', 'Europe/Moscow')).toBe(true);
    expect(validateSchedule('not-a-cron', 'Europe/Moscow')).toBe(false);
    expect(validateSchedule('0 9 * * *', 'Wrong/Timezone')).toBe(false);
  });

  it('calculates the next run in the selected timezone', () => {
    const next = getNextRun('0 9 * * *', 'Europe/Moscow', new Date('2026-08-27T04:00:00.000Z'));
    expect(next).toBe('2026-08-27T06:00:00.000Z');
  });

  it('returns ordered upcoming runs', () => {
    const values = getNextRuns('*/5 * * * *', 'UTC', 3);
    expect(values).toHaveLength(3);
    expect(values[0] < values[1] && values[1] < values[2]).toBe(true);
  });

  it('converts readable schedules to cron', () => {
    expect(scheduleToCron({mode:'interval',intervalMinutes:15})).toBe('*/15 * * * *');
    expect(scheduleToCron({mode:'weekdays',time:'09:30'})).toBe('30 9 * * 1-5');
    expect(scheduleToCron({mode:'weekly',time:'18:05',weekdays:[5,1,3]})).toBe('5 18 * * 1,3,5');
  });

  it('restores and describes a common cron schedule', () => {
    expect(scheduleFromCron('0 9 * * 1-5')).toEqual({mode:'weekdays',time:'09:00'});
    expect(describeSchedule('0 9 * * 1-5')).toBe('По будням в 09:00');
  });

  it('describes common advanced cron schedules in plain language', () => {
    expect(describeSchedule('*/30 * * * 1-5')).toBe('Каждые 30 минут по будням');
    expect(describeSchedule('0 */3 * * 0,6')).toBe('Каждые 3 часа по выходным');
    expect(describeSchedule('15 */2 * * *')).toBe('Каждые 2 часа, на 15-й минуте');
    expect(describeSchedule('30 8 1 * *')).toBe('Каждого 1-го числа в 08:30');
  });

  it('allows only five fields in advanced mode', () => {
    expect(()=>scheduleToCron({mode:'custom',cronExpression:'30 0 9 * * *'})).toThrow('пяти полей');
  });

  it('rejects a weekly schedule without selected days', () => {
    expect(()=>scheduleToCron({mode:'weekly',time:'09:00',weekdays:[]})).toThrow('хотя бы один день');
  });

  it('resolves an invalid schedule without throwing into the UI', () => {
    expect(resolveSchedule({mode:'weekly',time:'09:00',weekdays:[]})).toEqual({
      cronExpression:'',
      error:'Выберите хотя бы один день недели',
    });
  });
});
