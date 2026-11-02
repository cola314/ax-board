// flex.team 탭에서 javascript_tool 로 실행. 이번 주(월~오늘) 출퇴근을 [{date, in, out, off}] 로 돌려준다.
// 직접 열지 말고 `python -I scripts/secretary/config.py flex-js` 출력(사용자 키가 채워진 것)을 실행.
// 결과를 그대로 `python -I scripts/secretary/attendance_set.py` 의 stdin 으로 저장.
const USER = '__FLEX_USER_KEY__';  // config flex.user_key
const hm = ts => new Date(ts).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Seoul' });
const d = new Date(Date.now() + 9 * 3600e3), dow = (d.getUTCDay() + 6) % 7;
const mon = new Date(d - dow * 864e5).toISOString().slice(0, 10), fri = new Date(d - (dow - 4) * 864e5).toISOString().slice(0, 10);
const j = await (await fetch(`/api/v3/time-tracking/users/${USER}/work-schedules?from=${mon}&to=${fri}&timezone=Asia%2FSeoul`)).json();
const cs = await (await fetch(`/api/v2/time-tracking/work-clock/users/${USER}/current-status`)).json();
JSON.stringify((j.dailySchedules || []).map(s => {
  const work = s.timeBlocks.filter(b => b.type === 'WORK').map(b => b.value);
  const off = s.timeBlocks.filter(b => b.type !== 'WORK' && b.type !== 'REST').map(b => b.type).concat((s.dayOffs || []).map(o => o.dayOffType || o.type));
  const rec = s.date === cs.targetDate ? cs.targetDayWorkSchedule.workRecords : [];
  return {
    date: s.date,
    in: work[0] ? hm(work[0].startTimestamp.timestamp) : (rec[0] && rec[0].startTimestamp ? hm(rec[0].startTimestamp.timestamp) : null),
    out: work.length ? hm(work[work.length - 1].endTimestampExclusive.timestamp) : null,
    off: off.filter(Boolean),
  };
}));
