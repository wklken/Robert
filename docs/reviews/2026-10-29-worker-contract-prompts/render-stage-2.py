"""Render the unchanged 14 inputs; run with the chosen renderer revision installed.

Before was captured from the inherited stage-1 renderer before caller adaptation.
After: .venv/bin/python docs/reviews/2026-10-29-worker-contract-prompts/render-stage-2.py after
"""
import json
from pathlib import Path
import sys

from robert_agent.render_prompt import render_prompt

stage = sys.argv[1]
if stage not in {'before', 'after'}:
    raise SystemExit('expected before or after')
root = Path(__file__).resolve().parent
inputs = json.loads((root.parent / '2026-10-29-worker-prompts/inputs.json').read_text())
text = f'# Stage 2 {stage}: same 14 inputs, renderer {"before caller adaptation" if stage == "before" else "with compact caller adaptation"}\n\n'
for sample in inputs:
    prompt = render_prompt(sample['task'], sample['route_result'], sample['events'],
        sample.get('runtime_context'), sample.get('project_memories'), sample.get('runtime_knowledge'))
    text += '## ' + sample['name'] + '\n\n````text\n' + prompt + '````\n\n'
(root / f'stage-2-{stage}.md').write_text(text)
