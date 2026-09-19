from src.string_utils import stringify_dict

assert stringify_dict({1: "one", "nested": {2: "two"}}) == "1: one\nnested: \n2: two\n\n"
