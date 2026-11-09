"""
=============================================================================
Metin2 Balık Yapboz (Fishing Jigsaw) — Çözücü Çekirdeği (saf numpy, oyun bağımsız)
=============================================================================
Oyun kuralları (resmi wiki + ekran görüntülerinden doğrulandı):
  * Tahta 6 sütun x 4 satır = 24 hücre. Parçalar DÖNDÜRÜLEMEZ.
  * Her çekilen parça (sandık açma) 1 "adım" sayılır (Miktar sayacı). Parça ya tahtaya
    konur ya da sağ tık + onay ile atılır; ikisi de 1 adımdır.
  * Tahta dolunca adım sayısına göre ödül:  <=10 Büyük | 11-24 Orta | >=25 Küçük sandık.

Çözücü:
  * 7..N hücre dolduktan sonra (boş hücre <= exact_k) tahtanın TAMAMI için kesin
    (Bellman geriye doğru indirgeme) çözüm hesaplanır: her parça-şekli / adım sayısı için
    beklenen sandık değerini (Yang) maksimize eden yerleştir/at kararı verilir.
  * Başlangıç hamleleri (çok boş hücre) için topolojik sezgisel puan kullanılır
    (tek/çift hücrelik ölü cepler yaratmaz).
  * "box_cost" (λ): her çekilen sandığın maliyeti (milyon Yang cinsinden). λ=0 -> sadece
    büyük sandık şansını maksimize et, λ>0 -> sandık başına en yüksek verim.
=============================================================================
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

ROWS, COLS = 4, 6
NCELL = ROWS * COLS
FULL_MASK = (1 << NCELL) - 1

Cells = Tuple[Tuple[int, int], ...]
Placement = Tuple[int, int, int]  # (r0, c0, mask)  r0,c0 = şeklin sınır kutusunun sol-üst hücresi


def normalize_cells(cells: Sequence[Tuple[int, int]]) -> Cells:
    cl = [(int(r), int(c)) for r, c in cells]
    r0 = min(r for r, _ in cl)
    c0 = min(c for _, c in cl)
    return tuple(sorted((r - r0, c - c0) for r, c in cl))


def popcount(x: int) -> int:
    return x.bit_count()


_ROW0_MASK = 0x3F
_ROW3_MASK = 0x3F << 18
_COL0_MASK = sum(1 << (r * COLS) for r in range(ROWS))
_COL5_MASK = sum(1 << (r * COLS + (COLS - 1)) for r in range(ROWS))
_NOT_COL5_MASK = FULL_MASK ^ _COL5_MASK

_BIT_NEIGHBORS: Tuple[int, ...] = tuple(
    sum(
        1 << (nr * COLS + nc)
        for nr, nc in ((b // COLS - 1, b % COLS), (b // COLS + 1, b % COLS), (b // COLS, b % COLS - 1), (b // COLS, b % COLS + 1))
        if 0 <= nr < ROWS and 0 <= nc < COLS
    )
    for b in range(NCELL)
)


def cells_to_mask(cells: Sequence[Tuple[int, int]], r0: int = 0, c0: int = 0) -> int:
    m = 0
    for r, c in cells:
        m |= 1 << ((r + r0) * COLS + (c + c0))
    return m


def build_placements(cells: Cells) -> List[Placement]:
    h = max(r for r, _ in cells) + 1
    w = max(c for _, c in cells) + 1
    out: List[Placement] = []
    for r0 in range(ROWS - h + 1):
        for c0 in range(COLS - w + 1):
            out.append((r0, c0, cells_to_mask(cells, r0, c0)))
    return out


# --- Metin2 Resmi Balık Yapboz 6 Şekil + Deluxe Şekil ---
BUILTIN_SHAPES: List[Tuple[str, Cells]] = [
    ("Mavi I (3 dikey)", ((0, 0), (1, 0), (2, 0))),
    ("Turuncu 1x1", ((0, 0),)),
    ("Yeşil L", ((0, 0), (1, 0), (1, 1))),
    ("Sarı 7", ((0, 0), (0, 1), (1, 1))),
    ("Turkuaz 2x2", ((0, 0), (0, 1), (1, 0), (1, 1))),
    ("Kırmızı Z", ((0, 0), (0, 1), (1, 1), (1, 2))),
    ("Mor 2x3 (Deluxe)", ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2))),
]


class ShapeBook:
    """Bilinen parça şekilleri (normalize hücre kümeleri). Yeni şekil görülürse kalıcı olarak öğrenilir."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.names: List[str] = []
        self.shapes: List[Cells] = []
        self._pl: List[List[Placement]] = []
        self.version = 0
        for name, cells in BUILTIN_SHAPES:
            self._append(name, cells)
        self._load()

    def _append(self, name: str, cells: Cells) -> int:
        self.names.append(name)
        self.shapes.append(cells)
        self._pl.append(build_placements(cells))
        self.version += 1
        return len(self.shapes) - 1

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("learned", []):
                cells = normalize_cells(item["cells"])
                if cells not in self.shapes:
                    self._append(item.get("name", f"Öğrenilen {len(self.shapes) + 1}"), cells)
        except Exception:
            pass

    def save(self) -> None:
        if not self.path:
            return
        learned = [
            {"name": n, "cells": [list(c) for c in s]}
            for n, s in zip(self.names[len(BUILTIN_SHAPES):], self.shapes[len(BUILTIN_SHAPES):])
        ]
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"learned": learned}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def index_of(self, cells: Sequence[Tuple[int, int]], learn: bool = True) -> int:
        key = normalize_cells(cells)
        try:
            return self.shapes.index(key)
        except ValueError:
            if not learn:
                return -1
            idx = self._append(f"Yeni şekil ({len(key)} hücre)", key)
            self.save()
            return idx

    def placements(self, idx: int) -> List[Placement]:
        return self._pl[idx]

    def __len__(self) -> int:
        return len(self.shapes)


@dataclass
class Action:
    kind: str                 # 'place' | 'discard'
    r0: int = -1              # sınır kutusu sol-üst satır
    c0: int = -1              # sınır kutusu sol-üst sütun
    mask_after: int = 0
    value: float = 0.0
    note: str = ""


# ---------------------------------------------------------------------------
# Kesin çözücü (alt-uzay Bellman)
# ---------------------------------------------------------------------------
class ExactSolver:
    """Başlangıç maskesi `base_mask` olan tahta için, geri kalan boş k hücrenin tüm 2^k alt durumunda
    (adım sayısı m = 0..25) kesin en iyi karar tablosunu hesaplar."""

    def __init__(self, shape_pl: List[List[Placement]], base_mask: int,
                 r_large: float, r_med: float, r_small: float, box_cost: float):
        t0 = time.perf_counter()
        self.base_mask = base_mask
        self.r_large, self.r_med, self.r_small = float(r_large), float(r_med), float(r_small)
        self.box_cost = float(box_cost)
        self.nshape = len(shape_pl)

        empty_bits = [b for b in range(NCELL) if not (base_mask >> b) & 1]
        self.k = len(empty_bits)
        self.bit_to_local = {b: i for i, b in enumerate(empty_bits)}
        n_states = 1 << self.k
        self.n_states = n_states
        self.full_local = n_states - 1

        # yerel (sıkıştırılmış) yerleştirmeler
        self.local_pl: List[List[Tuple[int, int, int, int]]] = []  # (r0, c0, global_mask, local_mask)
        for pls in shape_pl:
            lst = []
            for r0, c0, pm in pls:
                if pm & base_mask:
                    continue
                lm = 0
                for b, i in self.bit_to_local.items():
                    if (pm >> b) & 1:
                        lm |= 1 << i
                lst.append((r0, c0, pm, lm))
            self.local_pl.append(lst)

        all_idx = np.arange(n_states, dtype=np.uint32)
        pc = np.zeros(n_states, dtype=np.uint8)
        tmp = all_idx.copy()
        for _ in range(self.k):
            pc += (tmp & 1).astype(np.uint8)
            tmp >>= 1

        # Geçerli durum/sonraki durum indeksleri (int32)
        self._valid: List[List[np.ndarray]] = []
        self._next: List[List[np.ndarray]] = []
        for lst in self.local_pl:
            vs, ns = [], []
            for _, _, _, lm in lst:
                v_idx = np.nonzero((all_idx & np.uint32(lm)) == 0)[0].astype(np.int32)
                vs.append(v_idx)
                ns.append((v_idx | np.int32(lm)).astype(np.int32))
            self._valid.append(vs)
            self._next.append(ns)

        INF = np.float32(1e6)
        n = self.nshape

        # 1) E[s]: 25+ adım rejiminde (sadece küçük sandık) tahtayı bitirmek için beklenen kalan çekiliş sayısı
        E = np.zeros(n_states, dtype=np.float32)
        for count in range(self.k - 1, -1, -1):
            idx = np.nonzero(pc == count)[0]
            if len(idx) == 0:
                continue
            idx_u = idx.astype(np.uint32)
            rows = []
            for si in range(n):
                best = np.full(len(idx), INF, dtype=np.float32)
                for _, _, _, lm in self.local_pl[si]:
                    valid = (idx_u & np.uint32(lm)) == 0
                    if valid.any():
                        cand = E[idx[valid] | lm]
                        best[valid] = np.minimum(best[valid], cand)
                rows.append(best)
            B = np.sort(np.vstack(rows), axis=0)
            csum = np.cumsum(B, axis=0)
            div = np.arange(1, n + 1, dtype=np.float32)[:, None]
            E[idx] = np.min((float(n) + csum) / div, axis=0)
        self.E = E

        # 2) V[m, s]: m adım yapılmışken beklenen (ödül - λ*kalan çekiliş) değeri
        V = np.empty((26, n_states), dtype=np.float32)
        bc = np.float32(self.box_cost)
        V[25, :] = np.float32(self.r_small) - bc * E
        V[25, self.full_local] = np.float32(self.r_small)
        inv_n = np.float32(1.0 / n)
        for m in range(24, -1, -1):
            v_next = V[m + 1].copy()
            v_next[self.full_local] = self._reward(m + 1)
            total = np.zeros(n_states, dtype=np.float32)
            for si in range(n):
                best_act = v_next.copy()  # atmak: aynı durum, m+1
                for v_idx, n_idx in zip(self._valid[si], self._next[si]):
                    best_act[v_idx] = np.maximum(best_act[v_idx], v_next[n_idx])
                total += best_act
            V[m] = total * inv_n - bc
            V[m, self.full_local] = self._reward(m)
        V[25, self.full_local] = np.float32(self.r_small)
        self.V = V
        self.solve_ms = (time.perf_counter() - t0) * 1000.0

    def _reward(self, moves: int) -> float:
        if moves <= 10:
            return self.r_large
        if moves <= 24:
            return self.r_med
        return self.r_small

    def to_local(self, mask: int) -> int:
        diff = mask ^ self.base_mask
        lm = 0
        for b, i in self.bit_to_local.items():
            if (diff >> b) & 1:
                lm |= 1 << i
        return lm

    def best_action(self, mask: int, moves_used: int, shape_idx: int) -> Action:
        """moves_used = şu anki parça HARİÇ yapılmış çekiliş sayısı."""
        ls = self.to_local(mask)
        lst = self.local_pl[shape_idx]
        if moves_used < 25:
            m_next = moves_used + 1
            vn = self.V[m_next]
            def val(local_state: int) -> float:
                if local_state == self.full_local:
                    return self._reward(m_next)
                return float(vn[local_state])
            best_val = val(ls)  # at
            best = None
            for r0, c0, pm, lm in lst:
                if ls & lm:
                    continue
                v = val(ls | lm)
                if v > best_val + 1e-6:
                    best_val, best = v, (r0, c0, pm)
            if best is None:
                return Action("discard", mask_after=mask, value=best_val, note="exact")
            return Action("place", best[0], best[1], mask | best[2], best_val, "exact")
        # >=25 adım: kalan beklenen çekilişi minimize et
        best_e = float(self.E[ls])
        best = None
        for r0, c0, pm, lm in lst:
            if ls & lm:
                continue
            e = 0.0 if (ls | lm) == self.full_local else float(self.E[ls | lm])
            if e < best_e - 1e-6:
                best_e, best = e, (r0, c0, pm)
        val = self.r_small - self.box_cost * (1.0 + best_e)
        if best is None:
            return Action("discard", mask_after=mask, value=val, note="exact25")
        return Action("place", best[0], best[1], mask | best[2], val, "exact25")


# ---------------------------------------------------------------------------
# Sezgisel puanlama (erken oyun)
# ---------------------------------------------------------------------------
def _components(empty: int) -> List[int]:
    comps: List[int] = []
    seen = 0
    rem = empty
    while rem:
        lsb = rem & -rem
        comp = 0
        stack = [lsb.bit_length() - 1]
        seen |= lsb
        while stack:
            cur = stack.pop()
            comp |= 1 << cur
            nbr = _BIT_NEIGHBORS[cur] & empty & ~seen
            while nbr:
                nb = nbr & -nbr
                seen |= nb
                stack.append(nb.bit_length() - 1)
                nbr ^= nb
        rem &= ~seen
        comps.append(comp)
    return comps


# Açılış kitabı: Boş tahtada (mask=0) her parça için beklenen değeri en yüksek kanıtlanmış yerleşim
OPENING_BOOK: Dict[int, Tuple[int, int]] = {
    0: (0, 5),  # Mavi I (3 dikey) -> en sağ sütuna dik
    1: (0, 5),  # Turuncu 1x1 -> köşe
    2: (2, 0),  # Yeşil L -> sol alt köşe
    3: (0, 4),  # Sarı 7 -> sağ üst köşe
    4: (0, 4),  # Turkuaz 2x2 -> sağ üst köşe
    5: (0, 0),  # Kırmızı Z -> sol üst
}


class JigsawPolicy:
    def __init__(self, book: ShapeBook, r_large: float = 16.5, r_med: float = 8.0, r_small: float = 4.0,
                 box_cost: float = 0.6, exact_k: int = 12, discard_bad_early: bool = True):
        self.book = book
        self.r_large, self.r_med, self.r_small = r_large, r_med, r_small
        self.box_cost = box_cost
        # <=12 boş hücre = 4096 durum (~10ms CPU). Eski 18 değeri (262144 durum) ani donmalara yol açıyordu.
        self.exact_k = min(14, max(6, int(exact_k)))
        self.discard_bad_early = discard_bad_early
        self._solver: Optional[ExactSolver] = None
        self._solver_book_ver = -1
        self._cache_book_ver = -1
        self._three_set: set[int] = set()
        self._large_pm_lists: List[Tuple[int, ...]] = []
        self.last_solve_ms = 0.0

    def reset(self) -> None:
        self._solver = None

    # -- yardımcılar ---------------------------------------------------------
    def _refresh_book_caches(self) -> None:
        if self._cache_book_ver == self.book.version:
            return
        three_s: set[int] = set()
        large_pms: List[Tuple[int, ...]] = []
        for i, s in enumerate(self.book.shapes):
            pms = tuple(pm for _, _, pm in self.book.placements(i))
            if len(s) == 3:
                three_s.update(pms)
            if len(s) >= 3 and i < 6:
                large_pms.append(pms)
        self._three_set = three_s
        self._large_pm_lists = large_pms
        self._cache_book_ver = self.book.version

    def _three_cell_masks(self) -> List[int]:
        self._refresh_book_caches()
        return list(self._three_set)

    def pocket_penalty(self, mask: int) -> Tuple[int, int, int]:
        """BFS ile (1x1 kör boşluk, 2'li çıkmaz cep, uymayan 3 hücrelik cep) sayıları."""
        self._refresh_book_caches()
        empty = (~mask) & FULL_MASK
        i1 = i2 = a3 = 0
        three = self._three_set
        for comp in _components(empty):
            sz = comp.bit_count()
            if sz == 1:
                i1 += 1
            elif sz == 2:
                i2 += 1
            elif sz == 3 and comp not in three:
                a3 += 1
        return i1, i2, a3

    def structural_score(self, mask: int) -> float:
        self._refresh_book_caches()
        filled = mask.bit_count()
        large_fits = 0
        diversity = 0
        for pms in self._large_pm_lists:
            cnt = 0
            for pm in pms:
                if not (mask & pm):
                    cnt += 1
            large_fits += cnt
            if cnt:
                diversity += 1
        i1, i2, a3 = self.pocket_penalty(mask)
        adj = (
            (((mask & _NOT_COL5_MASK) << 1) & mask).bit_count() * 2
            + ((mask << COLS) & mask).bit_count() * 2
            + (mask & _ROW0_MASK).bit_count()
            + (mask & _ROW3_MASK).bit_count()
            + (mask & _COL0_MASK).bit_count()
            + (mask & _COL5_MASK).bit_count()
        )
        return filled * 2.2 + large_fits * 0.34 + diversity * 1.8 + adj * 0.16 - i1 * 12.0 - i2 * 8.0 - a3 * 9.0

    def _ensure_solver(self, mask: int) -> ExactSolver:
        s = self._solver
        if (s is None or self._solver_book_ver != self.book.version
                or (mask & s.base_mask) != s.base_mask):
            # Standart 6 sandık parçası üzerinden Bellman hesapla (Deluxe 2x3 opsiyoneldir)
            base_shapes = min(6, len(self.book))
            shape_pl = [self.book.placements(i) for i in range(base_shapes)]
            s = ExactSolver(shape_pl, mask, self.r_large, self.r_med, self.r_small, self.box_cost)
            self._solver = s
            self._solver_book_ver = self.book.version
            self.last_solve_ms = s.solve_ms
        return s

    # -- ana karar -----------------------------------------------------------
    def choose(self, mask: int, moves_used: int, cells: Sequence[Tuple[int, int]]) -> Action:
        idx = self.book.index_of(cells, learn=True)
        empty_cnt = NCELL - popcount(mask)
        if empty_cnt == 0:
            return Action("discard", mask_after=mask, note="tahta dolu")

        # 0) İlk hamle: Açılış kitabı (boş tahtada kanıtlanmış en karlı konum)
        if mask == 0:
            if idx == 1 and self.discard_bad_early and moves_used <= 2:
                return Action("discard", mask_after=0, note="açılışta 1x1 atıldı (Büyük Sandık için)")
            if idx in OPENING_BOOK:
                r0, c0 = OPENING_BOOK[idx]
                for r, c, pm in self.book.placements(idx):
                    if r == r0 and c == c0:
                        return Action("place", r0, c0, pm, note="açılış kitabı")

        # 1) Kesin Bellman alt-uzay çözücüsü (k <= exact_k, varsayılan <=12 boş hücre ~10ms)
        if empty_cnt <= self.exact_k and idx < 6:
            return self._ensure_solver(mask).best_action(mask, moves_used, idx)

        # 2) Erken/Orta Oyun: Aday yerleşimler + BFS ölü cep kontrolü + 1-ply Lookahead
        cands: List[Tuple[float, int, int, int]] = []
        for r0, c0, pm in self.book.placements(idx):
            if mask & pm:
                continue
            nm = mask | pm
            if nm == FULL_MASK:
                return Action("place", r0, c0, nm, 1000.0, "tahtayı tamamlar")
            cands.append((self.structural_score(nm), r0, c0, nm))

        if not cands:
            return Action("discard", mask_after=mask, note="sığmıyor -> sağ tık çöpe at")

        cands.sort(key=lambda x: x[0], reverse=True)
        best_pick: Optional[Tuple[float, int, int, int]] = None
        for base_sc, r0, c0, nm in cands[:6]:
            exp_next = 0.0
            for next_idx in range(min(6, len(self.book))):
                best_n = base_sc - 1.5
                for _, _, npm in self.book.placements(next_idx):
                    if not (nm & npm):
                        sc2 = self.structural_score(nm | npm)
                        if sc2 > best_n:
                            best_n = sc2
                exp_next += best_n / 6.0
            total_sc = 0.35 * base_sc + 0.65 * exp_next
            if best_pick is None or total_sc > best_pick[0]:
                best_pick = (total_sc, r0, c0, nm)

        assert best_pick is not None
        best_sc, br, bc, bmask = best_pick

        if self.discard_bad_early and moves_used <= 6:
            before_dead = sum(self.pocket_penalty(mask))
            after_dead = sum(self.pocket_penalty(bmask))
            if after_dead > before_dead:
                return Action("discard", mask_after=mask, value=best_sc, note="BFS: 1x1 kör boşluk / ölü cep yaratır")

        return Action("place", br, bc, bmask, best_sc, "lookahead+bfs")


# ===========================================================================
# Bağımsız OOP NumPy + OpenCV HSV + Bitwise BFS Çözücü Sınıfı
# ===========================================================================
class JigsawActionKind(Enum):
    PLACE = "place"
    DISCARD = "discard"


@dataclass(frozen=True)
class JigsawShape:
    shape_id: int
    name: str
    cells: Tuple[Tuple[int, int], ...]
    hsv_ranges: Tuple[Tuple[Tuple[int, int, int], Tuple[int, int, int]], ...]


@dataclass
class JigsawMove:
    kind: JigsawActionKind
    row: int = -1
    col: int = -1
    score: float = 0.0
    reason: str = ""


class FishJigsawSolver:
    """
    6x4 NumPy matrisi, OpenCV cv2.inRange HSV şekil tanıma ve bitwise BFS kör
    boşluk analizini tek bağımsız sınıfta birleştiren yüksek hızlı çözücü.
    """

    ROWS = ROWS
    COLS = COLS
    NCELL = NCELL
    FULL_MASK = FULL_MASK

    SHAPES: Tuple[JigsawShape, ...] = (
        JigsawShape(1, "Blue_3x1",   ((0, 0), (1, 0), (2, 0)),         (((100, 150, 120), (130, 255, 255)),)),
        JigsawShape(2, "Orange_1x1", ((0, 0),),                         (((10,  150, 140), (23,  255, 255)),)),
        JigsawShape(3, "Green_L",    ((0, 0), (1, 0), (1, 1)),          (((40,  150, 140), (70,  255, 255)),)),
        JigsawShape(4, "Yellow_7",   ((0, 0), (0, 1), (1, 1)),          (((24,  150, 140), (38,  255, 255)),)),
        JigsawShape(5, "Cyan_2x2",   ((0, 0), (0, 1), (1, 0), (1, 1)),  (((80,  150, 140), (100, 255, 255)),)),
        JigsawShape(6, "Red_Z",      ((0, 0), (0, 1), (1, 1), (1, 2)),  (((0,   150, 140), (8,   255, 255)),
                                                                         ((172, 150, 140), (180, 255, 255)))),
    )

    _NEIGHBORS: Tuple[int, ...] = tuple(
        sum(
            1 << (nr * 6 + nc)
            for nr, nc in ((b // 6 - 1, b % 6), (b // 6 + 1, b % 6), (b // 6, b % 6 - 1), (b // 6, b % 6 + 1))
            if 0 <= nr < 4 and 0 <= nc < 6
        )
        for b in range(24)
    )

    def __init__(self, exact_k: int = 12) -> None:
        self._book = ShapeBook()
        self._policy = JigsawPolicy(self._book, exact_k=exact_k, discard_bad_early=True)
        self._shape_by_id = {s.shape_id: s for s in self.SHAPES}
        self._valid_3cell_masks: set[int] = set()
        for s in self.SHAPES:
            if len(s.cells) == 3:
                for _, _, pm in build_placements(s.cells):
                    self._valid_3cell_masks.add(pm)

    def reset_game(self) -> None:
        self._policy.reset()

    @staticmethod
    def matrix_to_bitmask(board: np.ndarray) -> int:
        bits = (board.reshape(24) > 0).astype(np.uint32)
        weights = 1 << np.arange(24, dtype=np.uint32)
        return int(np.dot(bits, weights))

    def read_board_matrix(
        self, frame_bgr: np.ndarray, grid_x0: int, grid_y0: int, cell_w: float = 39.6, cell_h: float = 39.5
    ) -> np.ndarray:
        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        board = np.zeros((self.ROWS, self.COLS), dtype=np.uint8)
        h_img, w_img = hsv.shape[:2]
        for r in range(self.ROWS):
            for c in range(self.COLS):
                cx = int(grid_x0 + (c + 0.5) * cell_w)
                cy = int(grid_y0 + (r + 0.5) * cell_h)
                if 6 <= cy < h_img - 6 and 6 <= cx < w_img - 6:
                    patch = hsv[cy - 6 : cy + 7, cx - 6 : cx + 7]
                    if float(patch[:, :, 1].mean()) > 80.0 and float(patch[:, :, 2].mean()) > 80.0:
                        board[r, c] = 1
        return board

    def recognize_piece_hsv(
        self, frame_bgr: np.ndarray, board_rect: Tuple[int, int, int, int], box_rect: Tuple[int, int, int, int]
    ) -> Optional[JigsawShape]:
        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        H, W = hsv.shape[:2]
        valid = np.full((H, W), 255, dtype=np.uint8)
        bx, by, bw, bh = board_rect
        sx, sy, sw, sh = box_rect
        valid[max(0, by) : min(H, by + bh), max(0, bx) : min(W, bx + bw)] = 0
        valid[max(0, sy) : min(H, sy + sh), max(0, sx) : min(W, sx + sw)] = 0

        best_shape: Optional[JigsawShape] = None
        best_area = 0.0
        for shape in self.SHAPES:
            mask = np.zeros((H, W), dtype=np.uint8)
            for lo, hi in shape.hsv_ranges:
                mask |= cv2.inRange(hsv, np.array(lo, dtype=np.uint8), np.array(hi, dtype=np.uint8))
            mask &= valid
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                area = cv2.contourArea(c)
                if area > 400 and area > best_area:
                    best_area = area
                    best_shape = shape
        return best_shape

    def bfs_blind_holes(self, board: np.ndarray) -> Tuple[int, int, int]:
        """(4, 6) matristeki boş hücrelerde BFS ile (1x1 kör delik, 2'li cep, kırık 3'lü) döndürür."""
        empty = (~self.matrix_to_bitmask(board)) & self.FULL_MASK
        visited = 0
        d1 = d2 = a3 = 0
        rem = empty
        while rem:
            lsb = rem & -rem
            comp = 0
            q = deque([lsb.bit_length() - 1])
            visited |= lsb
            while q:
                cur = q.popleft()
                comp |= 1 << cur
                nbr = self._NEIGHBORS[cur] & empty & ~visited
                while nbr:
                    nb = nbr & -nbr
                    visited |= nb
                    q.append(nb.bit_length() - 1)
                    nbr ^= nb
            rem &= ~visited
            sz = comp.bit_count()
            if sz == 1:
                d1 += 1
            elif sz == 2:
                d2 += 1
            elif sz == 3 and comp not in self._valid_3cell_masks:
                a3 += 1
        return d1, d2, a3

    def choose_move(self, board: np.ndarray, shape: JigsawShape, moves_used: int) -> JigsawMove:
        mask = self.matrix_to_bitmask(board)
        act = self._policy.choose(mask, moves_used, shape.cells)
        if act.kind == "place":
            return JigsawMove(JigsawActionKind.PLACE, act.r0, act.c0, act.value, act.note)
        return JigsawMove(JigsawActionKind.DISCARD, -1, -1, act.value, act.note)


# ---------------------------------------------------------------------------
# Simülasyon (test / ayar)
# ---------------------------------------------------------------------------
def simulate(policy: JigsawPolicy, n_games: int, seed: int = 1) -> Dict[str, float]:
    rng = random.Random(seed)
    large = med = small = 0
    total_moves = 0
    total_reward = 0.0
    nshapes = len(policy.book)
    for _ in range(n_games):
        policy.reset()
        mask, moves = 0, 0
        while mask != FULL_MASK:
            si = rng.randrange(nshapes)
            act = policy.choose(mask, moves, policy.book.shapes[si])
            moves += 1
            if act.kind == "place":
                mask = act.mask_after
        total_moves += moves
        if moves <= 10:
            large += 1
            total_reward += policy.r_large
        elif moves <= 24:
            med += 1
            total_reward += policy.r_med
        else:
            small += 1
            total_reward += policy.r_small
    return {
        "large%": 100.0 * large / n_games,
        "med%": 100.0 * med / n_games,
        "small%": 100.0 * small / n_games,
        "avg_moves": total_moves / n_games,
        "avg_chest": total_reward / n_games,
        "per_box": total_reward / total_moves,
    }

