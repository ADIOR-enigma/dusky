#!/usr/bin/env python3
import subprocess
import sys
import time

def run_test():
    prompts = [
        "Name a famous scientist in 1 short sentence.",
        "What was their most famous discovery in 1 short sentence?",
        "In what year was that discovery made?",
        "Give me 3 words summarizing their impact."
    ]

    print("==================================================", flush=True)
    print("STARTING 4-TURN EVENT-DRIVEN FOLLOW-UP TEST", flush=True)
    print("==================================================", flush=True)

    total_start = time.time()
    results = []

    for idx, p in enumerate(prompts, 1):
        print(f"\n[TURN {idx}] Prompt: '{p}'", flush=True)
        turn_start = time.time()
        
        # Execute bridge.py directly (event-driven completion)
        proc = subprocess.run(
            ["python3", "/home/dusk/.config/firefox_extentions/ai_bridge/bridge.py", p],
            capture_output=True,
            text=True
        )
        
        turn_duration = time.time() - turn_start
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        
        results.append((idx, p, turn_duration, out, proc.returncode))
        
        print(f"[TURN {idx} COMPLETED in {turn_duration:.2f}s | Return Code: {proc.returncode}]", flush=True)
        print(f"--> Response: {out}", flush=True)

    print("\n" + "="*50, flush=True)
    print("ALL 4 TURNS COMPLETED! SUMMARY TRANSCRIPT:", flush=True)
    print("="*50, flush=True)
    for idx, p, dur, out, rc in results:
        print(f"Turn {idx} ({dur:.1f}s): {p} => {out}", flush=True)
    print(f"\nTotal 4-turn execution time: {time.time() - total_start:.2f}s", flush=True)

if __name__ == "__main__":
    run_test()
