#!/usr/bin/env python3
"""Render VM results from preserved measurements; Python standard library only.

python3 docs/render_vm_figures.py --input-dir docs/measured-vm-2026-10-27 \
    --output-dir docs/measured-vm-2026-10-27/figures
"""
import argparse
import hashlib
import json
from pathlib import Path

from render_adaptive_figures import label, line


def render(root, output):
    sources = {}

    def read(relative):
        path = root / relative
        sources[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return json.loads(path.read_text())

    campaign = read(Path('report.json'))
    trials = {(t['repetition'], t['mode']): t for t in campaign['trials']}
    assert len(trials) == 6 and all(t['success'] for t in trials.values())
    r = read(Path('r1-adaptive/report.json'))
    q = read(Path('r1-adaptive/queue-final.json'))
    phases = read(Path('r1-adaptive/phase-events.json'))
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="900" viewBox="0 0 1280 900" role="img" aria-label="Résultats VM Linux : reprise, latence et adaptation mesurées">',
             '<rect width="1280" height="900" fill="#fafafa"/>', '<g font-family="Arial,Helvetica,sans-serif">',
             label(45, 45, 'SAWM Anti-Brouillage · Preuves VM Linux', size=27, weight='bold'),
             label(45, 75, '120/120 messages livrés et confirmés · 0 effet doublé · 6 essais · Émulation de paquets, pas de radio physique', size=16)]
    metrics = [('first_ack_after_recovery_ms', 'Premier ACK après retour du lien', 700),
               ('ack_p95_ms', 'Latence p95 jusqu’à l’ACK, par essai', 3000)]
    for col, (key, title, ymax) in enumerate(metrics):
        x, y, w, h = 100 + col * 610, 150, 475, 240
        parts += [label(x, 115, title, size=19, weight='bold'), label(x, 137, 'Millisecondes · plus bas = plus rapide', size=13, fill='#555')]
        py = lambda value: y + h - value / ymax * h
        for tick in [0, ymax / 2, ymax]:
            parts += [line(x, py(tick), x+w, py(tick), stroke='#ddd'), label(x-12, py(tick)+5, f'{tick:g}', size=13, anchor='end')]
        for rep in (1, 2, 3):
            center = x + 80 + (rep-1)*155
            for index, mode in enumerate(('static', 'adaptive')):
                value = trials[(rep, mode)]['metrics'][key]
                bx = center - 49 + index*51
                fill = '#aaa' if mode == 'static' else '#111'
                parts += [f'<rect x="{bx}" y="{py(value):.2f}" width="44" height="{y+h-py(value):.2f}" fill="{fill}"/>',
                          label(bx+22, py(value)-9, f'{value:.0f}', size=13, anchor='middle')]
            parts.append(label(center, y+h+25, f'Essai {rep}', size=14, anchor='middle'))
    parts += ['<rect x="460" y="437" width="20" height="14" fill="#aaa"/>', label(490, 450, 'Réglage fixe', size=14),
              '<rect x="650" y="437" width="20" height="14" fill="#111"/>', label(680, 450, 'Adaptatif', size=14),
              label(45, 490, 'Adaptation réellement enregistrée · exemple : essai 1 adaptatif', size=20, weight='bold')]
    origin = r['origin_monotonic_ns']
    attempts = {a['id']: a for a in q['attempts']}
    trace = q['summary']['adaptive']['calibration_trace']
    assert len(trace) == len(attempts)
    x, y, w, h, xmax, ymax = 100, 540, 1080, 210, 8, 1000
    px = lambda seconds: x + seconds/xmax*w
    py = lambda value: y+h-value/ymax*h
    for name, fill in [('degraded', '#ededed'), ('blackout', '#cecece')]:
        phase_index = next(i for i, p in enumerate(phases) if p['profile']['name'] == name)
        start = (phases[phase_index]['completed_monotonic_ns']-origin)/1e9
        end = (phases[phase_index+1]['started_monotonic_ns']-origin)/1e9
        parts += [f'<rect x="{px(start):.2f}" y="{y}" width="{px(end)-px(start):.2f}" height="{h}" fill="{fill}"/>',
                  label((px(start)+px(end))/2, y-10, 'Dégradé' if name == 'degraded' else 'Coupure', size=13, anchor='middle')]
    for tick in (0, 250, 500, 750, 1000):
        parts += [line(x, py(tick), x+w, py(tick), stroke='#ddd'), label(x-12, py(tick)+5, str(tick), size=13, anchor='end')]
    for tick in range(9):
        parts.append(label(px(tick), y+h+22, str(tick), size=13, anchor='middle'))
    for key, initial_key, stroke, dash in [('request_timeout_ms', 'request_timeout_ms', '#111', ''),
                                          ('retry_delay_ms', 'base_delay_ms', '#666', '6 4')]:
        initial = r['initial_policy'][initial_key]
        path = f'M {px(0):.2f} {py(initial):.2f}'
        prior_t = -1
        for event in trace:
            seconds = (attempts[event['attempt_id']]['finished_monotonic_ns']-origin)/1e9
            assert 0 <= seconds <= xmax and seconds >= prior_t
            prior_t = seconds
            path += f' H {px(seconds):.2f} V {py(event["new_"+key]):.2f}'
        parts.append(f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="2.6" stroke-dasharray="{dash}"/>')
    parts += [label(100, 520, 'Réglage (ms)', size=13), label(640, 800, 'Temps écoulé depuis le début de l’essai (secondes)', size=14, anchor='middle'),
              line(100, 828, 140, 828, stroke='#111', width=2.6), label(150, 833, 'Délai maximal d’une requête', size=14),
              line(510, 828, 550, 828, stroke='#666', width=2.6, dash='6 4'), label(560, 833, 'Attente avant nouvel essai', size=14),
              label(45, 866, 'Pertes aléatoires non appariées. Réglages locaux déterministes ; aucun nouvel appel Cloud. Le contrôleur ne connaît pas les phases à venir.', size=13, fill='#555'),
              label(45, 887, 'Adaptatif : 73 tentatives, fixe : 69. La reprise est plus rapide dans 2 essais sur 3 ; aucun avantage général de latence n’est démontré.', size=13, fill='#555'),
              '</g></svg>']
    output.mkdir(parents=True, exist_ok=False)
    figure = output/'vm-results.svg'
    figure.write_text('\n'.join(parts)+'\n')
    provenance = {'source_sha256': sources, 'renderer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'figure_sha256': {figure.name: hashlib.sha256(figure.read_bytes()).hexdigest()},
                  'comparison': 'All 3 repetitions, no pooled percentiles; calibration is repetition 1 only.'}
    (output/'provenance.json').write_text(json.dumps(provenance, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    render(args.input_dir, args.output_dir)
