import { spawn } from 'node:child_process';

const string = {type: 'string'};
const integer = {type: 'integer'};

export default function(pi) {
  const container = process.env.KANBAN_SANDBOX;
  if (!container || !/^kanban-[a-f0-9]+$/.test(container)) throw new Error('Missing task sandbox');
  const definitions = [
    ['read', 'Read a UTF-8 file in /workspace with numbered lines.', {path:string, offset:integer, limit:integer}, ['path']],
    ['list', 'List a directory in /workspace.', {path:string}, []],
    ['write', 'Write a UTF-8 file in /workspace.', {path:string, content:string}, ['path','content']],
    ['edit', 'Replace exactly one matching text fragment in a file.', {path:string, old_text:string, new_text:string}, ['path','old_text','new_text']],
    ['bash', 'Run a shell command in the isolated workspace. timeout is seconds (1–600). No network, host files, or GitHub credentials are available. Do not commit or push.', {command:string, timeout:integer}, ['command']],
  ];
  const allowed = process.env.KANBAN_READONLY === '1' ? ['read','list','bash'] : definitions.map(d=>d[0]);
  for (const [name, description, properties, required] of definitions) {
    if (!allowed.includes(name)) continue;
    pi.registerTool({
      name: `workspace_${name}`, label:name, description,
      parameters: {type:'object', properties, required, additionalProperties:false},
      async execute(_id, params, signal) {
        return await new Promise((resolve, reject) => {
          const child = spawn('docker', ['exec','-i',container,'python3','/kanban-tool.py'], {stdio:['pipe','pipe','pipe']});
          let output='', error='';
          const abort = () => child.kill('SIGKILL');
          signal?.addEventListener('abort', abort, {once:true});
          child.stdout.on('data', chunk => { if(output.length < 150000) output += chunk; });
          child.stderr.on('data', chunk => { if(error.length < 10000) error += chunk; });
          child.on('error', reject);
          child.on('close', code => {
            signal?.removeEventListener('abort', abort);
            if(signal?.aborted) return reject(new Error('Aborted'));
            if(code) return reject(new Error(error || `Sandbox exited ${code}`));
            let details;
            try { details = JSON.parse(output); } catch { return reject(new Error('Invalid sandbox result')); }
            resolve({content:[{type:'text',text:JSON.stringify(details)}], details, isError: !!details.error});
          });
          child.stdin.end(JSON.stringify({tool:name,...params}));
        });
      }
    });
  }
  pi.registerCommand('kanban-tools', {description: 'Inspect the task tools', handler: async (_args, ctx) => { ctx.ui.notify(JSON.stringify({container, tools: pi.getActiveTools()}), 'info'); }});
  pi.on('session_start', () => pi.setActiveTools(allowed.map(name=>`workspace_${name}`)));
  pi.on('tool_call', (event) => {
    if (!allowed.map(name=>`workspace_${name}`).includes(event.toolName)) return {block:true,reason:'Tool is outside this task'};
  });
}
