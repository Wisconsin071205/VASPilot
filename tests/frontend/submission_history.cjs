const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, {value:'cl9',innerHTML:'',textContent:'',disabled:false});
  return elements.get(id);
};
const ctx = {document:{getElementById:get, querySelector:get}, PROG_JOB:null,
  esc:String, badge:label=>label, fmtWhen:String, jobDuration:()=>'',jobEnd:()=>'', START_SRC:{},
  api:async action => action === 'job.list' ? {ok:true,jobs:[{job_id:'2',state:'RUNNING'}]} :
    {ok:true,active_job_ids:['2'],jobs:[
      {job_id:'129371',state:'UNKNOWN',directory:'/run',local_record:true},
      {job_id:'2',state:'UNKNOWN'}, {job_id:'3',state:'COMPLETED'}, {job_id:'4',state:'PENDING'}]}};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('const JOB_TERMINAL ='),html.indexOf('async function analyzeJob(')) +
  '\n' + html.slice(html.indexOf('async function loadJobs('),html.indexOf('// ISO 时间')),ctx);
(async () => {
  await ctx.loadJobs();
  const history = get('#rectable tbody').innerHTML;
  assert(history.includes('129371') && history.includes('状态待核对'));
  assert(history.includes('>3<') && history.includes('COMPLETED'));
  assert(!history.includes('>2<') && !history.includes('>4<'));
  assert(get('jobhistorynote').textContent.includes('保留提交记录'));
  assert(!history.includes('无已结束作业'));
  console.log('history retains unknown jobs and separates current active rows');
})().catch(e=>{console.error(e);process.exitCode=1;});
