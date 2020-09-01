import math

import torch.multiprocessing as mp
from Bio.Phylo.NewickIO import Parser

from .phylo import Phylogeny


def _handle_translate(lines, context):
    map_lines = [line.rstrip(",").split() for line in lines[1:-1]]
    context["translate"] = {key: value for key, value in map_lines}


def _handle_tree_newick(lines, context):
    assert len(lines) == 1
    tree, name, equal, newick = lines[0].split()
    assert tree == "tree"
    assert equal == "="
    tree = next(Parser.from_string(newick).parse())
    tree.name = name

    # Add translations as .comment attributes
    if "translate" in context:
        translate = context["translate"]
        for leaf in tree.get_terminals():
            leaf.comment = translate[leaf.name]

    return tree


def _handle_tree_torch(lines, context):
    assert len(lines) == 1
    tree, name, equal, newick = lines[0].split()
    assert tree == "tree"
    assert equal == "="
    tree = next(Parser.from_string(newick).parse())
    return Phylogeny.from_bio_phylo(tree)


def _handle_raw(lines, context):
    return lines, context


def read_nexus_trees(filename, *, format="newick", max_num_trees=math.inf,
                     processes=0):
    """
    Parse and iterate over newick trees stored in a nexus file.
    This streams the file and thus can handle larger files than
    ``Bio.Phylo.read(..., format="nexus")``.

    Returns an iterator of ``Bio.Phylo`` tree objects.
    """
    if format == "newick":
        context = {"translate": {}}
        handlers = {"translate": _handle_translate, "tree": _handle_tree_newick}
    elif format == "_raw_newick":
        context = {"translate": {}}
        handlers = {"translate": _handle_translate, "tree": _handle_raw}
    elif format == "torch":
        context = None
        handlers = {"tree": _handle_tree_torch}
    elif format == "_raw_torch":
        context = None
        handlers = {"tree": _handle_raw}
    else:
        raise ValueError(f"unknown format: {format}")

    if processes > 0:
        trees = read_nexus_trees(filename, format="_raw_" + format,
                                 max_num_trees=max_num_trees)
        with mp.Pool(processes) as pool:
            return pool.starmap(handlers["tree"], trees)

    with open(filename) as f:
        lines = iter(f)
        for line in lines:
            if line.startswith("Begin trees;"):
                break
        trees = []
        part = []
        for line in lines:
            line = line.strip()
            part.append(line)
            if not line.endswith(";"):
                continue
            type_ = part[0].split()[0].lower()
            handle = handlers.get(type_)
            if handle is not None:
                tree = handle(part, context)
                if tree is not None:
                    trees.append(tree)
                    if len(trees) == max_num_trees:
                        break
            part = []
        return trees


def stack_nexus_trees(filename, *, max_num_trees=math.inf, processes=0):
    """
    Loads a batch of trees from a nexus file.
    """
    trees = read_nexus_trees(filename, format="torch",
                             max_num_trees=max_num_trees,
                             processes=processes)
    return Phylogeny.stack(trees)
