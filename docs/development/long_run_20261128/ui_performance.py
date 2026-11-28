"""工具投影与行缓存微基准；不衡量模型或真实终端传输速度。"""
from pathlib import Path
import hashlib, json, os, statistics, sys, time, types, zipfile
sys.path.insert(0,str(Path.cwd()))
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from trina.console_ui import EnhancedConsoleInput
from trina.events import RuntimeEvent
from trina.event_view import EventTranscript
import argparse
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline-archive',type=Path,default=Path(__file__).with_name('baseline_source.zip'))
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
if args.output.exists(): parser.error('output must be a new path')
base=args.baseline_archive
with zipfile.ZipFile(base) as z:
    source=z.read('trina/event_view.py')
old=types.ModuleType('trina._baseline_event_view');old.__package__='trina';sys.modules[old.__name__]=old
exec(compile(source,'baseline/event_view.py','exec'),old.__dict__)
def measure(view_class,width,height):
    with create_pipe_input() as pipe, create_app_session(input=pipe,output=DummyOutput()):
        editor=EnhancedConsoleInput(status=lambda:'ready',running=lambda:True,dynamic_values=lambda *_:(),plain=True,motion='none')
        editor.buffer.text='draft\nsecond line'
        for index in range(100): editor.write(f'history {index}\n'*10)
        control=editor.transcript_control;control.create_content(width,height-8)
        view=view_class(editor.write,editor=editor)
        def consume(seq,kind,**payload):
            view.consume(RuntimeEvent(seq,'ui','ui',kind,time.time(),'tool','main','worker','one',payload))
        consume(1,'tool.started',tool='terminal_exec',params={'argv':['local-check']})
        elapsed=[]
        for index in range(120):
            start=time.perf_counter()
            consume(index+2,'tool.output',delta=f'line {index:03d}: '+('local output '*20)+'\n')
            control.create_content(width,height-8)
            elapsed.append((time.perf_counter()-start)*1000)
        assert editor.buffer.text=='draft\nsecond line'
        return {'total_ms':sum(elapsed),'p95_update_ms':sorted(elapsed)[int(len(elapsed)*.95)-1],
                'final_tool_rows':len(control.rows)-control.starts[-1], 'retained_chars':len(editor._chunks[-1])}
report={'method':'同一当前 console/viewport，替换会话开始的 event_view 投影；120 次输出与行排版，不含数据库、终端传输、模型',
        'baseline_event_view_sha256':hashlib.sha256(source).hexdigest(),
        'current_sha256':{name:hashlib.sha256((Path('trina')/name).read_bytes()).hexdigest() for name in ('console_ui.py','event_view.py','terminal.py')},'sizes':{}}
for width,height in ((80,24),(100,30),(120,36),(160,48)):
    samples={'before':[],'after':[]}
    for repeat in range(5):
        for arm in (('before','after') if repeat%2==0 else ('after','before')):
            samples[arm].append(measure(old.EventTranscript if arm=='before' else EventTranscript,width,height))
    report['sizes'][f'{width}x{height}']={arm:{'samples':rows,'median':{key:statistics.median(row[key] for row in rows) for key in rows[0]}} for arm,rows in samples.items()}
    print(width,height,{arm:row['median'] for arm,row in report['sizes'][f'{width}x{height}'].items()},flush=True)
args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
