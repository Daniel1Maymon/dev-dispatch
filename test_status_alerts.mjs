import fs from 'fs';

function extractEngine(file){
  const s = fs.readFileSync(file,'utf8');
  const start = s.indexOf('  /* ---------------------------------------------------------------------\n     Status-change alerts.');
  const end = s.indexOf('  function renderBoard(', start);
  if (start < 0 || end < 0) throw new Error('engine block not found in '+file);
  return s.slice(start, end);
}

function makeEnv(){
  const store = {};
  const beeps = [];
  const els = [];
  const env = {
    localStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k,v) => { store[k] = String(v); },
    },
    window: { addEventListener(){}, AudioContext: function(){ return { state:'running' }; } },
    document: { querySelectorAll: () => els },
    $: () => ({ style:{} }),
    setTimeout: () => 0, clearTimeout: () => {},
    _beeps: beeps, _store: store, _els: els,
  };
  return env;
}

function build(file){
  const env = makeEnv();
  // real engine source, verbatim from the page; beep() is swapped for a counter so the
  // test can assert on it without an audio device
  const src = extractEngine(file).replace(
    /function beep\(\)\{[\s\S]*?\n  \}/,
    'function beep(){ _beeps.push(Date.now()); }'
  );
  const keys = Object.keys(env);
  const fn = new Function(...keys, src + '\n; return { noteChanges, flashing, FLASH, setSuppress: () => { suppressAlert = true; } };');
  return { api: fn(...keys.map(k => env[k])), env };
}

let fails = 0;
function check(name, got, want){
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
}

// ---------------- reviews.html ----------------
{
  const { api, env } = build('reviews.html');
  const card = (url, st, pend, auth, reply, verdict) =>
    ({url, state:st, pendingReview:pend, awaitingAuthor:auth, needsReply:reply, myVerdict:verdict});
  const A = card('u/1','OPEN',false,true,false,'COMMENTED');
  const B = card('u/2','OPEN',false,true,false,'COMMENTED');

  api.noteChanges([A,B]);
  check('reviews: first ever run is silent', env._beeps.length, 0);
  check('reviews: first run flashes nothing', api.FLASH.size, 0);

  api.noteChanges([A,B]);
  check('reviews: identical poll is silent', env._beeps.length, 0);

  const A2 = card('u/1','OPEN',true,false,true,'COMMENTED');   // author replied -> your court
  api.noteChanges([A2,B]);
  check('reviews: status moved -> one beep', env._beeps.length, 1);
  check('reviews: only the moved card flashes', [...api.FLASH.keys()], ['u/1']);
  check('reviews: unmoved card not flashing', api.flashing('u/2'), false);

  api.noteChanges([A2,B]);
  check('reviews: settled again -> no extra beep', env._beeps.length, 1);

  const C = card('u/3','OPEN',true,false,false,null);
  api.noteChanges([A2,B,C]);
  check('reviews: brand-new PR counts as a change', env._beeps.length, 2);
}

// ---------------- tracker.html ----------------
{
  const { api, env } = build('tracker.html');
  const board = (s1, s2) => ([{taskId:'t1', repos:[{id:'r1', status:s1},{id:'r2', status:s2}]}]);

  api.noteChanges(board('working on it','waiting for review'));
  check('tracker: first ever run is silent', env._beeps.length, 0);

  api.noteChanges(board('working on it','waiting for review'));
  check('tracker: identical poll is silent', env._beeps.length, 0);

  api.noteChanges(board('working on it','ready to merge'));
  check('tracker: pill moved -> one beep', env._beeps.length, 1);
  check('tracker: only that row flashes', [...api.FLASH.keys()], ['t1:r2']);

  api.noteChanges(board('ready to merge','ready to merge'));
  check('tracker: second row moves -> second beep', env._beeps.length, 2);

  // the "" trap: an empty status must be a real value, not "unknown"
  api.noteChanges(board('', 'ready to merge'));
  check('tracker: -> "" counts as a change', env._beeps.length, 3);
  api.noteChanges(board('', 'ready to merge'));
  check('tracker: "" -> "" is silent', env._beeps.length, 3);
  api.noteChanges(board('', 'ready to merge'));
  check('tracker: "" stays silent on repeat', env._beeps.length, 3);
}


// ---------------- one beep per refresh, not per card ----------------
{
  const { api, env } = build('tracker.html');
  const board = (a,b,c) => ([{taskId:'t1', repos:[{id:'r1',status:a},{id:'r2',status:b},{id:'r3',status:c}]}]);
  api.noteChanges(board('x','x','x'));                 // baseline
  api.noteChanges(board('y','y','y'));                 // all three move at once
  check('batch: 3 rows move -> exactly 1 beep', env._beeps.length, 1);
  check('batch: all 3 rows flash', [...api.FLASH.keys()].sort(), ['t1:r1','t1:r2','t1:r3']);
}

// ---------------- your own dropdown edit must not beep ----------------
{
  const { api, env } = build('tracker.html');
  const board = s => ([{taskId:'t1', repos:[{id:'r1', status:s}]}]);
  api.noteChanges(board('working on it'));             // baseline
  // mimic the change handler: suppressAlert = true, then renderBoard -> noteChanges
  api.setSuppress();
  api.noteChanges(board('ready to merge'));
  check('self-edit: no beep for a status you set yourself', env._beeps.length, 0);
  check('self-edit: no flash either', api.FLASH.size, 0);
  // and the baseline was still updated, so the next server poll agreeing is silent
  api.noteChanges(board('ready to merge'));
  check('self-edit: server echoing it back is silent', env._beeps.length, 0);
  // but a genuine move after that still fires
  api.noteChanges(board('working on it'));
  check('self-edit: gate re-arms after one render', env._beeps.length, 1);
}

console.log(fails ? `\n${fails} FAILURE(S)` : '\nall checks passed');
process.exit(fails ? 1 : 0);
