import json
import csv
import sys
import random
import math
import time
import numpy as np
import importlib.util

"""
spec = importlib.util.spec_from_file_location("opine", "opine_obj.py")
opinedb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(opinedb)
"""
sys.path.append('.')
import opine_obj


def generate_queries(query_terms,
                     n=100,
                     num_subjective=3,
                     obj_attributes=None,
                     num_objective=2):
    results = []
    for i in range(n):
        query = random.choices(query_terms, k=num_subjective)
        if obj_attributes != None:
            objs = random.choices(list(obj_attributes.keys()), k=num_objective)
            for attr in objs:
                op = ''
                val = ''
                attr_type = obj_attributes[attr]['type']
                if attr_type == 'bool':
                    bit = random.randint(0, 1)
                    op = '='
                    val = 'True' if bit == 1 else 'False'
                elif attr_type == 'cate':
                    op = '='
                    val = random.choice(obj_attributes[attr]['values'])
                else: # 'num'
                    op = random.choice(['<', '>'])
                    val = random.randint(int(obj_attributes[attr]['range'][0]),
                                         int(obj_attributes[attr]['range'][1]))
                    val = str(val)

                query.append('%s:%s %s %s' % (attr_type, attr, op, val))
        results.append(query)
    return results


def AB_baseline_hotel(entities):
    ab_attributes = ['Location',
                  'Cleanliness', 'Staff',
                  'Comfort', 'Facilities',
                  'Value for Money',
                  'Breakfast', 'Free Wifi']
    rank_rating_attr1 = 'Price on Oct. 31'
    rank_rating_attr2 = 'Overall Rating'

    attributes = ab_attributes
    bids = list(entities.keys())
    list1 = sorted(bids, key=lambda x : -entities[x][rank_rating_attr1])[:10]
    list2 = sorted(bids, key=lambda x : -entities[x][rank_rating_attr2])[:10]

    list_size1 = []
    for attr in attributes:
        list_size1.append(sorted(bids, key=lambda x : -entities[x][attr])[:10])

    list_size2 = []
    for attr1 in attributes:
        for attr2 in attributes:
            list_size2.append(sorted(bids, key=lambda x : -entities[x][attr1]-entities[x][attr2])[:10])

    return [[list1], [list2], list_size1, list_size2]


def AB_baseline_restaurant(entities):
    ab_attributes = json.load(open('data/yelp_attributes.json'))
    rank_rating_attr1 = 'stars'
    rank_rating_attr2 = 'review_count'

    attributes = ab_attributes
    bids = list(entities.keys())
    list1 = sorted(bids, key=lambda x : -entities[x][rank_rating_attr1])[:10]
    list2 = sorted(bids, key=lambda x : -entities[x][rank_rating_attr2])[:10]

    list_size1 = []
    for attr in attributes:
        sub_bids = [bid for bid in bids if attr in entities[bid]]
        list_size1.append(sorted(sub_bids, key=lambda x : -entities[x][rank_rating_attr1])[:10])

    list_size2 = []
    for attr1 in attributes:
        for attr2 in attributes:
            sub_bids = [bid for bid in bids if attr1 in entities[bid] and attr2 in entities[bid]]
            list_size2.append(sorted(sub_bids, key=lambda x : -entities[x][rank_rating_attr1])[:10])

    return [[list1], [list2], list_size1, list_size2]



def IR_baseline(entities, queries, entity_type='hotel'):

    rank_rating_attr2 = 'review_count' if entity_type == 'restaurant' else 'Overall Rating'

    from gensim.summarization.bm25 import get_bm25_weights, BM25
    import gensim

    def build_index(hotels):
        # build bm25 model
        for hname in hotels:
            hotels[hname]['combined_review'] = ''
            for rid in hotels[hname]['reviews']:
                if 'text' in reviews[rid]:
                    review = reviews[rid]['text'].strip()
                else:
                    review = reviews[rid]['review'].strip()
                hotels[hname]['combined_review'] += ' ' + review

        # build bm25 index
        corpus = [gensim.utils.simple_preprocess(hotels[h]['combined_review']) for h in hotels]
        bm25 = BM25(corpus)
        return bm25

    def baseline(query, hotels, merged_query=True, num_synonyms=0):
        result = []
        bm25_scores = []

        if merged_query:
            qterms = []
            for qterm in query:
                qterm = qterm.lower()
                for q in qterm.split(' '):
                    qterms += [q]
                    if num_synonyms > 0 and q in model.wv:
                        for (w, score) in model.wv.most_similar(q, topn=num_synonyms):
                            qterms.append(w)
            query = ' '.join(qterms)
            # print(query)
            bm25_scores = bm25.get_scores(gensim.utils.simple_preprocess(query))
            # bm25_scores = bm25.get_scores(gensim.utils.simple_preprocess(query), 0.5)
        else:
            for qterm in query:
                qterm = qterm.lower()
                words = ''
                if num_synonyms > 0:
                    for wo in qterm.split(' '):
                        if wo in model.wv:
                            for (w, score) in model.wv.most_similar(q, topn=num_synonyms):
                                words += ' ' + w
                scores = bm25.get_scores(gensim.utils.simple_preprocess(qterm + words))
                # scores = bm25.get_scores(gensim.utils.simple_preprocess(qterm + words), 0.5)
                if len(bm25_scores) == 0:
                    bm25_scores = list(scores)
                else:
                    for i in range(len(scores)):
                        bm25_scores[i] += scores[i]
        hid = 0
        for hname in hotels:
            score = bm25_scores[hid] * float(hotels[hname][rank_rating_attr2])
            result.append((hname, score))
            hid += 1
        return sorted(result, key=lambda x : -x[1])[:10]

    bm25 = build_index(entities)
    results = []
    for query in queries:
        pairs = baseline(query, entities)
        bids = [p[0] for p in pairs]
        results.append(bids)
    return results


def read_groundtruth(query_label_fn):
    ground_truth = {}
    for (bid, _, qterm, res) in json.load(open(query_label_fn)):
        if bid in entities:
            qterm = qterm.lower()
            ground_truth[(bid, qterm)] = 1.0 if res == 'yes' else 0.0
    return ground_truth


def read_queries(query_fn):
    queries = open(query_fn).read().splitlines()
    queries = [q.lower() for q in queries]
    return queries

def get_query_score(bid, query, ground_truth):
    total = 0.0
    for qterm in query:
        score = 0.0
        if (bid, qterm) in ground_truth:
            score = ground_truth[(bid, qterm)]
        # objective
        if ':' in qterm:
            attr, op, oprand = qterm.split(' ')
            atype, attr = attr.split(':')
            if attr in entities[bid]:
                score = 0
                if atype == 'bool' or atype == 'cate':
                    scores = 1 if oprand.lower() == entities[bid][attr].lower() else 0
                elif atype == 'num':
                    if op == '<':
                        score = 1 if float(oprand) > float(entities[bid][attr]) else 0
                    else:
                        score = 1 if float(oprand) < float(entities[bid][attr]) else 0
        total += score
    return total


def discounted_cumulative_gain(query, ranklist, ground_truth):
    score = 0.0
    for (i, bid) in enumerate(ranklist):
        score += get_query_score(bid, query, ground_truth) / math.log2(i + 2)
    return score

all_bids = [] # set([])
previous_query = None
previous_max_dcg = 0.0
def normalized_discounted_cumulative_gain(query, ranklist, ground_truth):
    global previous_query
    global previous_max_dcg
    global all_bids
    # find max rank
    if query == previous_query:
        max_score = previous_max_dcg
    else:
        if len(all_bids) == 0:
            # bid_set = set([])
            # for (bid, _) in ground_truth:
            #     bid_set.add(bid)
            # all_bids += list(bid_set)
            all_bids += list(entities.keys())

        bids = all_bids
        scores = {}
        for bid in bids:
            scores[bid] = get_query_score(bid, query, ground_truth)

        bids = sorted(bids, key=lambda x : -scores[x])[:10]
        max_score = discounted_cumulative_gain(query, bids, ground_truth)
        # debug
        # print(query, bids, max_score)
        previous_max_dcg = max_score
        previous_query = query

    score = discounted_cumulative_gain(query, ranklist, ground_truth)
    if max_score == 0:
        return 1.0
    return score / max_score


def run_experiment(entities, opine, entity_type='hotel', seed=123):
    random.seed(seed)
    np.random.seed(seed)
    simple_queries = generate_queries(query_terms, n=100,
                                      num_subjective=3,
                                      obj_attributes=opine.obj_attrs,
                                      num_objective=3)
    medium_queries = generate_queries(query_terms, n=100,
                                      num_subjective=5,
                                      obj_attributes=opine.obj_attrs,
                                      num_objective=5)
    hard_queries = generate_queries(query_terms, n=100,
                                    num_subjective=7,
                                    obj_attributes=opine.obj_attrs,
                                    num_objective=7)
    result_table = [[0] * 3 for _ in range(4)]
    column_id = 0

    if entity_type == 'hotel':
        ab_results = AB_baseline_hotel(entities)
    else:
        ab_results = AB_baseline_restaurant(entities)

    ds_names = ['easy', 'medium', 'hard']
    for ds_name, queries in zip(ds_names, [simple_queries, medium_queries, hard_queries]):
        print(ds_name)
        # debug:
        # queries = queries[:5]

        # IR
        ir_results = IR_baseline(entities, queries, entity_type)
        scores = []
        for (query, ranklist) in zip(queries, ir_results):
            scores.append(normalized_discounted_cumulative_gain(query, ranklist, ground_truth))
        print('IR-based\t%f' % (sum(scores) / len(scores)))

        # AB
        scores = [[] for _ in range(4)]
        for query in queries:
            results = []
            for ranklists in ab_results:
                score = 0.0
                for ranklist in ranklists:
                    new_score = normalized_discounted_cumulative_gain(query, ranklist, ground_truth)
                    score = max(score, new_score)
                results.append(score)
            # print(query, results)
            for i in range(4):
                scores[i].append(results[i])
        for i, score_list in enumerate(scores):
            print('AB method %d\t%f' % (i, sum(score_list) / len(score_list)))

        # run opine
#         for mode_context in ['marker_sensitive',
#                              'marker_agnostic',
#                              'marker_boolean',
#                              'marker_ignore']:
        for mode_context in ['marker_sensitive',
                             'histogram_sensitive',
                             'marker_agnostic',
                             'marker_boolean',
                             'marker_ignore']:
            mode, context = mode_context.split('_')
            opine.clear_cache()
            start_time = time.time()
            scores = []
            for query in queries:
                ranklist = opine.opine(query, bids=list(entities.keys()), mode=mode, obj_context=context)[:10]
                scores.append(normalized_discounted_cumulative_gain(query, ranklist, ground_truth))
#                 print(query, [(bid, get_query_score(bid, query, ground_truth)) for bid in ranklist],
#                       discounted_cumulative_gain(query, ranklist, ground_truth), scores[-1])

            run_time = time.time() - start_time
            quality = sum(scores) / len(scores)
            print('Opine - %s, score = %f' % (mode_context, quality))
            print('Opine - %s, running time = %f' % (mode_context, run_time))

        print()


def read_entities(entity_fn, histogram_fn, obj_fn=None):
    entities = json.load(open(entity_fn))
    histograms = json.load(open(histogram_fn))
    for ent in entities:
        bid = ent['business_id']
        if bid in histograms:
            for key in histograms[bid]:
                ent[key] = histograms[bid][key]
    entities = {ent['business_id'] : ent for ent in entities}

    # reading objective predicates
    if obj_fn != None:
        with open(obj_fn) as fin:
            reader = csv.DictReader(fin)
            for row in reader:
                bid = row['ID']
                if bid not in entities:
                    continue
                for attr in row:
                    if attr == 'ID':
                        continue
                    val = row[attr]
                    attr_type, attr = attr.split(':')
                    if attr_type == 'cate':
                        entities[bid][attr] = val
                    elif attr_type == 'num':
                        entities[bid][attr] = float(val)
                    else:
                        entities[bid][attr] = val.lower()

    print('num entities =\t%d' % len(entities))
    return entities


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: python evaluate_obj.py dataset')
        exit()

    dataset = sys.argv[1]
    if len(sys.argv) == 3:
        seed = int(sys.argv[2])
    else:
        seed = 123

    if dataset == 'amsterdam' or dataset == 'london':
        path = 'data/%s/' % dataset
        entity_type = 'hotel'
        query_fn = path + 'hotel_queries.txt'
        label_fn = path + 'labels.json'
        entity_fn = 'data/raw_%s_hotels.json' % dataset
        histogram_fn = path + 'entities_with_histograms.json'
        extraction_fn = path + '%s_reviews_with_extractions.json' % dataset
        phrase_sentiment_fn = path + 'sentiment.json'
        word2vec_fn = path + 'word2vec.model'
        idf_fn = path + 'idf.json'
        labels_fn = path + 'labels.json'
        obj_fn = path + 'obj_%s.csv' % dataset
    elif dataset == 'toronto_lp' or dataset == 'toronto_jp':
        path = 'data/toronto/'
        entity_type = 'restaurant'
        query_fn = path + 'restaurant_queries.txt'
        label_fn = path + 'labels.json'
        rest_type = dataset.split('_')[1]
        entity_fn = 'data/raw_%s_restaurants.json' % rest_type
        histogram_fn = path + rest_type + '_entities_with_histograms.json'
        extraction_fn = path + rest_type + '_restaurant_reviews_with_extractions.json'
        phrase_sentiment_fn = path + 'sentiment.json'
        word2vec_fn = path + 'word2vec.model'
        idf_fn = path + 'idf.json'
        labels_fn = path + 'labels.json'
        obj_fn = path + 'obj_toronto.csv'

    entities = read_entities(entity_fn, histogram_fn, obj_fn=obj_fn) # json.load(open(histogram_fn))
    reviews = json.load(open(extraction_fn))
    reviews = { review['review_id'] : review for review in reviews }
    query_terms = read_queries(query_fn)
    ground_truth = read_groundtruth(label_fn)
    op = opine_obj.SimpleOpine(histogram_fn, extraction_fn, phrase_sentiment_fn, word2vec_fn, idf_fn, labels_fn, obj_fn)

    run_experiment(entities, op, entity_type, seed=seed)
