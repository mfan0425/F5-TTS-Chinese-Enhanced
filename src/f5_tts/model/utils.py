from __future__ import annotations

import os
import random
from collections import defaultdict
from importlib.resources import files

import jieba
import torch
from pypinyin import Style, lazy_pinyin
from torch.nn.utils.rnn import pad_sequence
from pypinyin import Style
from pypinyin_g2pw import G2PWPinyin
import re


# seed everything


def seed_everything(seed=0):
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


# helpers


def exists(v):
    return v is not None


def default(v, d):
    return v if exists(v) else d


def is_package_available(package_name: str) -> bool:
    try:
        import importlib

        package_exists = importlib.util.find_spec(package_name) is not None
        return package_exists
    except Exception:
        return False


# tensor helpers


def lens_to_mask(t: int["b"], length: int | None = None) -> bool["b n"]:  # noqa: F722 F821
    if not exists(length):
        length = t.amax()

    seq = torch.arange(length, device=t.device)
    return seq[None, :] < t[:, None]


def mask_from_start_end_indices(seq_len: int["b"], start: int["b"], end: int["b"]):  # noqa: F722 F821
    max_seq_len = seq_len.max().item()
    seq = torch.arange(max_seq_len, device=start.device).long()
    start_mask = seq[None, :] >= start[:, None]
    end_mask = seq[None, :] < end[:, None]
    return start_mask & end_mask


def mask_from_frac_lengths(seq_len: int["b"], frac_lengths: float["b"]):  # noqa: F722 F821
    lengths = (frac_lengths * seq_len).long()
    max_start = seq_len - lengths

    rand = torch.rand_like(frac_lengths)
    start = (max_start * rand).long().clamp(min=0)
    end = start + lengths

    return mask_from_start_end_indices(seq_len, start, end)


def maybe_masked_mean(t: float["b n d"], mask: bool["b n"] = None) -> float["b d"]:  # noqa: F722
    if not exists(mask):
        return t.mean(dim=1)

    t = torch.where(mask[:, :, None], t, torch.tensor(0.0, device=t.device))
    num = t.sum(dim=1)
    den = mask.float().sum(dim=1)

    return num / den.clamp(min=1.0)


# simple utf-8 tokenizer, since paper went character based
def list_str_to_tensor(text: list[str], padding_value=-1) -> int["b nt"]:  # noqa: F722
    list_tensors = [torch.tensor([*bytes(t, "UTF-8")]) for t in text]  # ByT5 style
    text = pad_sequence(list_tensors, padding_value=padding_value, batch_first=True)
    return text


# char tokenizer, based on custom dataset's extracted .txt file
def list_str_to_idx(
    text: list[str] | list[list[str]],
    vocab_char_map: dict[str, int],  # {char: idx}
    padding_value=-1,
) -> int["b nt"]:  # noqa: F722
    list_idx_tensors = [torch.tensor([vocab_char_map.get(c, 0) for c in t]) for t in text]  # pinyin or char style
    text = pad_sequence(list_idx_tensors, padding_value=padding_value, batch_first=True)
    return text


# Get tokenizer


def get_tokenizer(dataset_name, tokenizer: str = "pinyin"):
    """
    tokenizer   - "pinyin" do g2p for only chinese characters, need .txt vocab_file
                - "char" for char-wise tokenizer, need .txt vocab_file
                - "byte" for utf-8 tokenizer
                - "custom" if you're directly passing in a path to the vocab.txt you want to use
    vocab_size  - if use "pinyin", all available pinyin types, common alphabets (also those with accent) and symbols
                - if use "char", derived from unfiltered character & symbol counts of custom dataset
                - if use "byte", set to 256 (unicode byte range)
    """
    if tokenizer in ["pinyin", "char"]:
        tokenizer_path = os.path.join(files("f5_tts").joinpath("../../data"), f"{dataset_name}_{tokenizer}/vocab.txt")
        with open(tokenizer_path, "r", encoding="utf-8") as f:
            vocab_char_map = {}
            for i, char in enumerate(f):
                vocab_char_map[char[:-1]] = i
        vocab_size = len(vocab_char_map)
        assert vocab_char_map[" "] == 0, "make sure space is of idx 0 in vocab.txt, cuz 0 is used for unknown char"

    elif tokenizer == "byte":
        vocab_char_map = None
        vocab_size = 256

    elif tokenizer == "custom":
        with open(dataset_name, "r", encoding="utf-8") as f:
            vocab_char_map = {}
            for i, char in enumerate(f):
                vocab_char_map[char[:-1]] = i
        vocab_size = len(vocab_char_map)

    return vocab_char_map, vocab_size


# convert char to pinyin
def convert_char_to_pinyin(text_list, polyphone=True, g2pw = None, isDebug = False):
    if jieba.dt.initialized is False:
        jieba.default_logger.setLevel(50)  # CRITICAL
        jieba.initialize()

    if g2pw is None:
        g2pw = G2PWPinyin(model_dir='G2PWModel/',
                          model_source='bert-base-chinese/',
                          v_to_u=False, neutral_tone_with_five=False)

    final_text_list = []
    final_text_list_whole = []
    custom_trans = str.maketrans(
        {";": ",", "“": '"', "”": '"', "‘": "'", "’": "'"}
    )  # add custom trans here, to address oov

    def is_chinese(c):
        return (
            "\u3100" <= c <= "\u9fff"  # common chinese characters
        )
    TONE3_STYLE_PATTERN = re.compile(r"^[a-zA-Z]+[1-5]?$")

    def is_tone3_style(text: str) -> bool:
        """
        Detects if a string is in the 'Style.TONE3' pinyin format (e.g., 'hua4').
        The format is one or more letters followed by a single tone digit (1-5).
        Returns:
        True if the string matches the pattern, False otherwise.
        """
        # Ensure the input is a string, otherwise it can't match.
        if not isinstance(text, str):
            return False
        
        return TONE3_STYLE_PATTERN.match(text) is not None
    
    for text in text_list:
        char_list = []
        char_list_whole = []
        text = text.translate(custom_trans)
        if isDebug:
            print(f"tranlated text: {text}")
        # g2pw采用整句推理
        sentence = g2pw.lazy_pinyin(text, neutral_tone_with_five=False, style=Style.TONE3)
        filtered_sentence_raw = [
            item for item in sentence 
            if is_tone3_style(item)
        ]
        # translate a pinyin which cannot pronouce well
        translation_map = {
            'shei2': 'shui2',
        }
        filtered_sentence = [translation_map.get(item, item) for item in filtered_sentence_raw]

        if isDebug:
            print(f"filtered sentence: {filtered_sentence}")
        chinese_char_len = len(filtered_sentence)
        j = 0
        for seg in jieba.cut(text):
            seg_byte_len = len(bytes(seg, "UTF-8"))
            if seg_byte_len == len(seg):  # if pure alphabets and symbols
                if char_list and seg_byte_len > 1 and char_list[-1] not in " :'\"":
                    char_list.append(" ")
                    char_list_whole.append(" ")
                char_list.extend(seg)
                char_list_whole.extend(seg)
            elif polyphone and seg_byte_len == 3 * len(seg):  # if pure east asian characters
                seg_ = lazy_pinyin(seg, style=Style.TONE3, tone_sandhi=True)
                for i, c in enumerate(seg):
                    chinese = is_chinese(c)
                    if chinese:
                        char_list.append(" ")
                        char_list_whole.append(" ")

                    char_list.append(seg_[i])
                    if chinese:
                        if (j < chinese_char_len):
                            char_list_whole.append(filtered_sentence[j])
                            if isDebug:
                                print(f"adding {filtered_sentence[j]}")
                        j = j + 1
                    else:
                        char_list_whole.append(seg_[i])
                    
            else:  # if mixed characters, alphabets and symbols
                for c in seg:
                    if ord(c) < 256:
                        char_list.extend(c)
                        char_list_whole.extend(c)
                    elif is_chinese(c):
                        char_list.append(" ")
                        char_list_whole.append(" ")
                        char_list.extend(lazy_pinyin(c, style=Style.TONE3, tone_sandhi=True))
                        if (j < chinese_char_len):
                            char_list_whole.append(filtered_sentence[j])
                            if isDebug:
                                print(f"adding {filtered_sentence[j]}")
                        j = j + 1
                    else:
                        char_list.append(c)
                        char_list_whole.append(c)
        final_text_list.append(char_list)
        final_text_list_whole.append(char_list_whole)
    
    if (j != len(filtered_sentence)) :
        print(f"length different original: {j} new: {len(filtered_sentence)}")
        return final_text_list
    else:
        if isDebug:
            print(f"original pinyin:{final_text_list}")
            print(f"return whole sentence pinyin: {final_text_list_whole}")
        return final_text_list_whole


# filter func for dirty data with many repetitions


def repetition_found(text, length=2, tolerance=10):
    pattern_count = defaultdict(int)
    for i in range(len(text) - length + 1):
        pattern = text[i : i + length]
        pattern_count[pattern] += 1
    for pattern, count in pattern_count.items():
        if count > tolerance:
            return True
    return False


# get the empirically pruned step for sampling


def get_epss_timesteps(n, device, dtype):
    dt = 1 / 32
    predefined_timesteps = {
        5: [0, 2, 4, 8, 16, 32],
        6: [0, 2, 4, 6, 8, 16, 32],
        7: [0, 2, 4, 6, 8, 16, 24, 32],
        10: [0, 2, 4, 6, 8, 12, 16, 20, 24, 28, 32],
        12: [0, 2, 4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32],
        16: [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 20, 24, 28, 32],
    }
    t = predefined_timesteps.get(n, [])
    if not t:
        return torch.linspace(0, 1, n + 1, device=device, dtype=dtype)
    return dt * torch.tensor(t, device=device, dtype=dtype)
