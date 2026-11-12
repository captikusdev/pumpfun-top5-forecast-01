import json
import math
import os
import random
import sys
from datetime import datetime

def parse_iso8601(s):
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return datetime.fromisoformat(s)

def main():
    input_dir = os.environ.get('MAKEFUN_INPUT_DIR', '/input')
    output_dir = os.environ.get('MAKEFUN_OUTPUT_DIR', '/output')

    task_path = os.path.join(input_dir, 'task.json')
    history_path = os.path.join(input_dir, 'history.ndjson')
    output_path = os.path.join(output_dir, 'forecast.json')

    if not os.path.exists(task_path):
        print(f"Error: {task_path} not found", file=sys.stderr)
        return

    with open(task_path, 'r') as f:
        task = json.load(f)

    edges = task['size']['edges']
    num_places = task.get('places', 5)

    # 1. Parse all marks
    mint_series = {} # mint -> list of (t_seconds, rank, logcap, venue)
    marks = []
    
    if os.path.exists(history_path):
        with open(history_path, 'r') as f:
            for line in f:
                if not line.strip(): continue
                try:
                    mark = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if mark.get('kind') != 'mark': continue
                
                t = parse_iso8601(mark['at']).timestamp()
                marks.append({'t': t, 'mark': mark})
                
                for row in mark.get('rows', []):
                    mint = row['mint']
                    if mint not in mint_series:
                        mint_series[mint] = []
                    
                    market_cap = float(row['market_cap_lamports'])
                    logcap = math.log10(market_cap) - 9
                    mint_series[mint].append((t, row['rank'], logcap, row.get('venue', 'curve')))

    if not marks:
        result = {"probabilities": [], "top5": [], "sizes": []}
        os.makedirs(output_dir, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(result, f)
        return

    marks.sort(key=lambda x: x['t'])
    last_mark = marks[-1]['mark']
    last_t = marks[-1]['t']
    
    # 2. Candidates
    last_12_mints = set()
    for m in marks[-12:]:
        for row in m['mark'].get('rows', []):
            last_12_mints.add(row['mint'])
            
    top_30_last = set()
    for row in last_mark.get('rows', []):
        if row['rank'] <= 30:
            top_30_last.add(row['mint'])
            
    candidate_mints = last_12_mints | top_30_last
    
    # 3. Parameters
    candidates = []
    rank1_mu_ref = 5.0
    
    # Map for easy lookup of rank in last mark
    last_mark_ranks = {row['mint']: row['rank'] for row in last_mark.get('rows', [])}

    for mint in candidate_mints:
        series = mint_series[mint]
        last_entry = series[-1]
        L = last_entry[2]
        venue = last_entry[3]
        
        trend = 0.0
        for entry in reversed(series[:-1]):
            if last_t - entry[0] >= 50 * 60:
                trend = L - entry[2]
                break
            
        graduated = (venue == "pool")
        mu = L + max(-0.15, min(0.20, 0.35 * trend)) + (0.0 if graduated else -0.22)
        
        candidates.append({
            'mint': mint,
            'mu': mu,
            'sigma': 0.62,
            'last_rank': last_mark_ranks.get(mint, 999)
        })
        
        if last_mark_ranks.get(mint) == 1:
            rank1_mu_ref = mu

    # 4. Simulate
    rng = random.Random(20261112)
    num_draws = 6000
    
    p_tally = {c['mint']: [0]*6 for c in candidates}
    num_bins = len(edges) + 1
    size_tally = [[0]*num_bins for _ in range(6)]
    
    for _ in range(num_draws):
        draw = []
        for c in candidates:
            draw.append({'mint': c['mint'], 'cap': 10**rng.gauss(c['mu'], c['sigma']), 'phantom': False})
        for _ in range(6):
            draw.append({'mint': None, 'cap': 10**rng.gauss(rank1_mu_ref - 0.55, 0.75), 'phantom': True})
            
        draw.sort(key=lambda x: x['cap'], reverse=True)
        
        for p in range(1, 6):
            if p <= len(draw):
                winner = draw[p-1]
                if not winner['phantom']:
                    p_tally[winner['mint']][p] += 1
                
                log_val = math.log10(winner['cap'])
                bin_idx = 0
                for i, edge in enumerate(edges):
                    if log_val >= edge:
                        bin_idx = i + 1
                    else:
                        break
                size_tally[p][bin_idx] += 1

    # 5. Emit
    probabilities = []
    mint_masses = []
    for mint in candidate_mints:
        mass = sum(p_tally[mint][1:6]) / num_draws
        if mass > 0:
            max_p = -1
            modal = 1
            for p in range(1, 6):
                prob = p_tally[mint][p] / num_draws
                if prob >= 0.004:
                    probabilities.append({"mint": mint, "place": p, "p": prob})
                if p_tally[mint][p] > max_p:
                    max_p = p_tally[mint][p]
                    modal = p
            mint_masses.append({
                'mint': mint,
                'mass': mass,
                'modal': modal,
                'p1': p_tally[mint][1] / num_draws,
                'last_rank': last_mark_ranks.get(mint, 999)
            })

    for p in range(1, 6):
        place_probs = [item for item in probabilities if item['place'] == p]
        s = sum(item['p'] for item in place_probs)
        if s > 0.85:
            for item in place_probs: item['p'] *= (0.85 / s)
                
    sizes = []
    for p in range(1, 6):
        smoothed = [h + 0.5 for h in size_tally[p]]
        s = sum(smoothed)
        for b, val in enumerate(smoothed):
            sizes.append({"place": p, "bin": b, "p": (val / s) * 0.97})
            
    mint_masses.sort(key=lambda x: -x['mass'])
    top5_candidates = mint_masses[:5]
    top5_candidates.sort(key=lambda x: (x['modal'], -x['p1'], x['last_rank']))
    top5 = [m['mint'] for m in top5_candidates]
    
    if len(top5) < 5:
        last_rows = sorted(last_mark.get('rows', []), key=lambda x: x['rank'])
        for row in last_rows:
            if row['mint'] not in top5:
                top5.append(row['mint'])
                if len(top5) >= 5: break
    
    result = {"probabilities": probabilities, "top5": top5[:5], "sizes": sizes}
    
    # 6. Validate
    for p in range(1, 6):
        assert sum(i['p'] for i in result['probabilities'] if i['place'] == p) <= 1.0001
        assert sum(i['p'] for i in result['sizes'] if i['place'] == p) <= 1.0001
    assert len(set(result['top5'])) == 5
    
    os.makedirs(output_dir, exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(result, f)
    
    if os.path.getsize(output_path) > 1000000:
        print("Warning: size > 1MB", file=sys.stderr)

if __name__ == "__main__":
    main()
