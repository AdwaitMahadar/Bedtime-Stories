"""Name ideas for characters the parent didn't name.

Left alone, the model reuses a few favourites in story after story (in testing, Pip, Pim and Biscuit in
about one story in three, including "Sir Biscuit" and "Mistletoe Pim"). Banning names in the prompt
backfired: the writer started a banned name and corrected itself mid-story ("Pim, no, not Pim.").
So code offers a few random, uncommon names instead, and the writer may use them or invent others.
"""

from __future__ import annotations

import random
from typing import Optional

NAMES = (
    "Agatha", "Alder", "Amos", "Anouk", "Arlo", "Astrid", "Barnaby", "Basil", "Bea", "Bertram",
    "Bramble", "Calloway", "Cassius", "Cleo", "Clementine", "Cosmo", "Dahlia", "Delphine", "Dorian",
    "Edith", "Elio", "Esme", "Ezra", "Fennel", "Ferdinand", "Fig", "Florentine", "Gideon", "Greta",
    "Gus", "Hamish", "Harriet", "Hazel", "Horatio", "Ida", "Ignatius", "Imogen", "Inigo", "Isadora",
    "Jasper", "Juniper", "Kasimir", "Kit", "Lark", "Leopold", "Linnea", "Lorcan", "Lupin", "Mabel",
    "Magnus", "Marigold", "Matilda", "Merritt", "Minnow", "Montague", "Nell", "Niamh", "Nico",
    "Octavia", "Odette", "Olbert", "Opal", "Orla", "Osric", "Otis", "Pemberton", "Percival", "Petra",
    "Pippa", "Primrose", "Quentin", "Quill", "Rafferty", "Ramona", "Rosalind", "Rufus", "Saffron",
    "Seraphina", "Sorrel", "Sybil", "Tamsin", "Tansy", "Thaddeus", "Theodora", "Thistle", "Tobias",
    "Ulla", "Ursula", "Valentina", "Vesper", "Wilbur", "Winifred", "Wolfgang", "Wren", "Xavier",
    "Yara", "Yusuf", "Zadie", "Zephyr", "Zinnia", "Aurelio", "Bodhi", "Calla", "Dmitri", "Elowen",
    "Faye", "Gwendolyn", "Hector", "Iris", "Jory", "Kiran", "Lulu", "Mateo", "Noor", "Oona",
    "Priya", "Rumi", "Sana", "Tariq", "Umi", "Vivi", "Wenceslas", "Yuki", "Zuri",
)


def name_ideas(k: int = 5, rng: Optional[random.Random] = None) -> list[str]:
    """`k` different names, chosen at random."""
    return (rng or random).sample(NAMES, k)
