"""Confere o RTP dos jogos. Rodar:  python simular_rtp.py"""
import slots, random
print("Fruity Jackpot - RTP exato: %.2f%%" % (slots.rtp_slot() * 100))
n = 300000; tot = 0; hit = 0
for _ in range(n):
    w = slots.evaluate(slots.spin_grid(), 1000)[0]; tot += w; hit += w > 0
print("  simulado (%d giros): RTP %.2f%% | acerto %.1f%%" % (n, tot / n / 10, hit / n * 100))
n2 = 400000; t2 = 0; h2 = 0
for _ in range(n2):
    w = slots.tg_eval(slots.tg_spin(), 1000)[0]; t2 += w; h2 += w > 0
print("Tigre Dourado - simulado (%d giros): RTP %.2f%% | acerto %.1f%%" % (n2, t2 / n2 / 10, h2 / n2 * 100))
for alvo in (1.5, 2, 5, 10):                       # Aviator: qualquer alvo de saque devolve ~RTP
    g = sum(1 for _ in range(n) if slots.sortear_crash() >= alvo * 100)
    print("Aviator saque %.1fx: acerta %.1f%% -> RTP %.2f%%" % (alvo, g / n * 100, g / n * alvo * 100))
import mines
for m_, k_ in ((3, 5), (10, 3), (24, 1)):          # Mines: chance de limpar k casas x multiplicador = RTP
    p = 1.0
    for i in range(k_): p *= (25 - m_ - i) / (25 - i)
    print("Mines %d minas, sacar apos %d casas: %.2fx x chance %.1f%% -> RTP %.2f%%" % (m_, k_, mines.mult(m_, k_), p * 100, p * mines.mult(m_, k_) * 100))
print("Aviator: RTP configurado %.1f%% (lucro da casa %.1f%%)" % (slots.AV_RTP * 100, 100 - slots.AV_RTP * 100))
