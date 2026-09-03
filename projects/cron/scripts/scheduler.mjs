const baseUrl = process.env.AI_CRON_URL || 'http://127.0.0.1:3000';

async function tick() {
  try {
    const response = await fetch(`${baseUrl}/api/scheduler`, { method: 'POST' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    if (result.processed) console.log(`[scheduler] Запущено задач: ${result.processed}`);
  } catch {
    // Сервер может запускаться дольше scheduler-а; следующий tick повторит запрос.
  }
}

setTimeout(tick, 1500);
setInterval(tick, 10_000);
