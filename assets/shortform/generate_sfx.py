"""Synthesize a small royalty-free SFX pack (no external files/licensing).
Run: uv run python generate.py   → whoosh.wav, pop.wav, click.wav (then mp3)
"""
import numpy as np, wave, struct
SR = 44100

def save(name, y):
    y = y / (np.max(np.abs(y)) + 1e-9)
    y = np.tanh(y * 1.1)                       # soft clip
    pcm = (y * 0.85 * 32767).astype(np.int16)
    with wave.open(name, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print("wrote", name, f"{len(y)/SR:.2f}s")

def onepole_sweep(x, a):                         # time-varying one-pole lowpass
    y = np.empty_like(x); prev = 0.0
    for i in range(len(x)):
        prev += a[i] * (x[i] - prev); y[i] = prev
    return y

# WHOOSH — filtered noise, cutoff opens then closes, smooth amplitude hump
n = int(0.45 * SR); t = np.linspace(0, 1, n)
noise = np.random.randn(n)
a = 0.02 + 0.38 * np.sin(np.pi * t) ** 1.2       # cutoff low→high→low
whoosh = onepole_sweep(noise, a)
env = np.sin(np.pi * t) ** 1.4
save("whoosh.wav", whoosh * env)

# POP — short tonal blip with a pitch drop + tiny transient
n = int(0.14 * SR); t = np.linspace(0, 0.14, n)
f = 680 * np.exp(-t * 7)                          # 680→~250 Hz
tone = np.sin(2 * np.pi * np.cumsum(f) / SR)
trans = np.random.randn(n) * np.exp(-t * 120) * 0.4
pop = (tone + trans) * np.exp(-t * 26)
save("pop.wav", pop)

# CLICK — very short transient
n = int(0.03 * SR); t = np.linspace(0, 0.03, n)
click = np.random.randn(n) * np.exp(-t * 260)
save("click.wav", click)
