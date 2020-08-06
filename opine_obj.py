import sys
import os
import json
import csv
import gensim
import math
import random
import numpy as np

from gensim.models import Word2Vec
from gensim.summarization.bm25 import get_bm25_weights, BM25

from scipy import spatial
from sklearn.neighbors import KDTree
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from nltk.sentiment.vader import SentimentIntensityAnalyzer


class CooccurInterpreter:
    """
    The co-occurrence query interpreter. When a query predicate is not similar
    to any linguistic variant, we look at positive reviews that matches the query
    predicate and find subjective attributes extracted from those reviews.
    Attributes:
        reviews (Dict): a dictionary from review_id to the review objects
        review_ids (List): the list of all review ids
        interpret_cache (Dict): a dictionary caching interpretation results
        position_index (Dict): an index for fast look-up of position of tokens
        idf (Dict): stores the idf of each attribute
    """
    def __init__(self, reviews):
        reviews = { review['review_id'] : review for review in reviews }
        self.reviews = reviews
        self.review_ids = []
        self.interpret_cache = {}
        self.position_index = {}
        self.idf = {}

        def build_index():
            # build bm25 index
            corpus = []
            total = 0.0
            SIA = SentimentIntensityAnalyzer()
            for rid in reviews:
                self.review_ids.append(rid)
                if 'text' in reviews[rid]:
                    sent = reviews[rid]['text']
                else:
                    sent = reviews[rid]['review']

                tokens = gensim.utils.simple_preprocess(sent.lower())
                reviews[rid]['sentiment'] = SIA.polarity_scores(sent)['compound']

                corpus.append(tokens)
                self.position_index[rid] = {}
                for (pos, token) in enumerate(tokens):
                    if token not in self.position_index[rid]:
                        self.position_index[rid][token] = [pos]
                    else:
                        self.position_index[rid][token].append(pos)

                for ext in reviews[rid]['extractions']:
                    attr = ext['attribute']
                    if attr not in self.idf:
                        self.idf[attr] = 1
                    else:
                        self.idf[attr] += 1
                    total += 1
            bm25 = BM25(corpus)
            for attr in self.idf:
                self.idf[attr] = math.log2(total / self.idf[attr])
            return bm25
        self.bm25 = build_index()


    def get_dist(self, position_index, phrase1, phrase2):
        """
        Compute the distance of two phrases in a single review as the
        minimal distance among the tokens in the two phrases.
        Args:
            position_index (Dict): an index of a review for position look-up
            phrase1 (string): the first phrase
            phrase2 (string): the second phrase
        Returns:
            int: the distance
        """
        tokens1 = gensim.utils.simple_preprocess(phrase1.lower())
        tokens2 = gensim.utils.simple_preprocess(phrase2.lower())

        positions1 = []
        for token1 in tokens1:
            if token1 in position_index:
                positions1 += position_index[token1]

        positions2 = []
        for token2 in tokens2:
            if token2 in position_index:
                positions2 += position_index[token2]

        result = -1
        for p1 in positions1:
            for p2 in positions2:
                if result < 0 or abs(p1 - p2) < result:
                    result = abs(p1 - p2)
        return result


    def interpret(self, qterm, debug=False):
        """
        Compute with the co-occurrence interpreter.
        Args:
            qterm (string): the query term to be interpreted
            debug (boolean): whether print debug info
        Returns:
            string: the best matching predicate (None if not found)
            string: the corresponding phrase
        """
        if qterm in self.interpret_cache:
            return self.interpret_cache[qterm]

        scores = self.bm25.get_scores(gensim.utils.simple_preprocess(qterm))
        # scores = self.bm25.get_scores(gensim.utils.simple_preprocess(qterm), 0.5)
        score_mp = {}
        for (i, rid) in enumerate(self.review_ids):
            if scores[i] > 0:
                score_mp[rid] = scores[i] * self.reviews[rid]['sentiment']
            else:
                score_mp[rid] = 0.0

        sorted_review_ids = sorted(self.review_ids, key=lambda x : -score_mp[x])
        attribute_scores = {}
        represented_phrases = {}

        for rid in sorted_review_ids[:10]:
            if score_mp[rid] <= 0:
                continue
            extractions = self.reviews[rid]['extractions']
            if debug:
                if 'text' in self.reviews[rid]:
                    print(self.reviews[rid]['text'])
                else:
                    print(self.reviews[rid]['review'])
            min_dist = -1
            min_dist_phrase = ''
            min_dist_attr = None
            for ext in extractions:
                phrase = ext['predicate'] + ' ' + ext['entity']
                dist = self.get_dist(self.position_index[rid], phrase, qterm)
                if dist >= 0 and (min_dist < 0 or dist < min_dist):
                    min_dist = dist
                    min_dist_phrase = phrase
                    min_dist_attr = ext['attribute']

            if debug:
                print(min_dist_attr, min_dist_phrase, min_dist)

            if min_dist_attr != None:
                if min_dist_attr not in attribute_scores:
                    represented_phrases[min_dist_attr] = min_dist_phrase
                    attribute_scores[min_dist_attr] = 1
                else:
                    attribute_scores[min_dist_attr] += 1

        best_attr_score = 0.0
        best_attr = None
        for attr in attribute_scores:
            attribute_scores[attr] *= self.idf[attr]
            if attribute_scores[attr] > best_attr_score:
                best_attr_score = attribute_scores[attr]
                best_attr = attr

        if best_attr == None:
            return None, None
        return best_attr, represented_phrases[best_attr]



class SimpleOpine:
    """
    The in-memory version of OpineDB. Query interpretation is done with a combination
    of the word2vec method (nearest neighbor) and the co-occurrence method.
    Each query predicate is scored by a logistic regression model.

    Attributes:
        entities (Dict): a dictionary where each item is an entity
        model (Word2Vec): a word2vec model trained on reviews
        idf (Dict): a dictionary of the pre-computed idf of each token
        phrase2vec_cache (Dict): cache for already computed phrase vectors
        all_phrases (List): the list of all extracted phrases (for query interpretation)
        membership_cache (Dict): cache the attribute score
        interpret_cache (Dict): cache the query interpretation results
    """

    def __init__(self, histogram_fn,
                 extraction_fn,
                 phrase_sentiment_fn,
                 word2vec_fn,
                 idf_fn,
                 query_label_fn,
                 obj_attr_fn=None,
                 entity_fn=None):
        if entity_fn == None:
            self.entities = json.load(open(histogram_fn))
            self.reviews = json.load(open(extraction_fn))
        else:
            bids = set([])
            raw_entities = json.load(open(entity_fn))
            for row in raw_entities:
                bids.add(row['business_id'])
            # filtering
            self.entities = json.load(open(histogram_fn))
            self.entities = { bid : self.entities[bid] for bid in bids }
            self.reviews = json.load(open(extraction_fn))
            self.reviews = [review for review in self.reviews if review['business_id'] in bids]

        # reading objective attributes
        self.obj_attrs = {}
        self.cate_to_id = {}
        if obj_attr_fn != None:
            with open(obj_attr_fn) as fin:
                reader = csv.DictReader(fin)
                for row in reader:
                    bid = row['ID']
                    if bid not in self.entities:
                        continue
                    for attr in row:
                        if attr == 'ID':
                            continue
                        val = row[attr]
                        attr_type, attr = attr.split(':')
                        if attr not in self.obj_attrs:
                            self.obj_attrs[attr] = {'type': attr_type}
                            if attr_type == 'cate':
                                self.obj_attrs[attr]['values'] = [val]
                            if attr_type == 'num':
                                self.obj_attrs[attr]['range'] = [float(val), float(val)]

                        if attr_type == 'cate':
                            self.entities[bid][attr] = val
                            if val not in self.obj_attrs[attr]['values']:
                                self.obj_attrs[attr]['values'].append(val)
                            if val not in self.cate_to_id:
                                self.cate_to_id[val] = len(self.cate_to_id)
                        elif attr_type == 'num':
                            self.entities[bid][attr] = float(val)
                            self.obj_attrs[attr]['range'][0] = \
                                min(self.obj_attrs[attr]['range'][0], float(val))
                            self.obj_attrs[attr]['range'][1] = \
                                max(self.obj_attrs[attr]['range'][1], float(val))
                        else:
                            self.entities[bid][attr] = val.lower()

        # load files and initialize attributes
        self.phrase_sentiments = json.load(open(phrase_sentiment_fn))
        self.model = Word2Vec.load(word2vec_fn)
        self.idf = json.load(open(idf_fn))
        self.phrase2vec_cache = {}
        self.phrase_mp = {}
        self.all_phrases = []
        self.all_vectors = []
        self.membership_cache = {}
        self.interpret_cache = {}

        # index for the w2v method for query interpretation
        def build_NN_index():
            for bid in self.entities:
                histogram = self.entities[bid]['histogram']
                for attr in histogram:
                    for phrase in histogram[attr]:
                        phrase = phrase.lower()
                        if phrase not in self.phrase_mp:
                            self.phrase_mp[phrase] = len(self.phrase_mp)
                            self.all_phrases.append((attr, phrase))
                            self.all_vectors.append(self.phrase2vec(phrase))

            return KDTree(self.all_vectors, leaf_size=40)

        self.kd_tree = build_NN_index()

        # index for the co-occurrence method
        self.cooc = CooccurInterpreter(self.reviews)

        def train_scorer(num_samples=1500):
            ground_truth = {}
            all_bids = set([])
            all_qterms = set([])
            for (bid, _, qterm, res) in json.load(open(query_label_fn)):
                if bid in self.entities:
                    ground_truth[(bid, qterm)] = 1.0 if res == 'yes' else 0.0
                    all_bids.add(bid)
                    all_qterms.add(qterm)
            all_bids = list(all_bids)
            all_qterms = list(all_qterms)
            X_phrases = []
            X_summary = []
            y_phrases = []
            y_summary = []
            sampled = 0
            while sampled < num_samples:
                bid = random.choice(all_bids)
                qterm = random.choice(all_qterms)
                attr_name, _ = self.interpret(qterm)
                if attr_name in self.entities[bid]['summaries']:
                    sampled += 1
                    # print(bid, attr_name, qterm)
                    if len(self.obj_attrs) == 0:
                        # no objective attributes
                        X_phrases.append(self.get_features_phrases(self.entities[bid]['histogram'][attr_name], qterm))
                        X_summary.append(self.get_features_summary(self.entities[bid]['summaries'][attr_name], qterm))
                        if (bid, qterm) in ground_truth and ground_truth[(bid, qterm)] > 0:
                            y_phrases.append(1)
                            y_summary.append(1)
                        else:
                            y_phrases.append(0)
                            y_summary.append(0)
                    else:
                        # create examples for objective+subjective
                        # create a dummy example (subjective only)
                        obj_features = self.get_features_obj('', '', '', '', '')
                        X_phrases.append(np.concatenate((obj_features,
                                         self.get_features_phrases(self.entities[bid]['histogram'][attr_name], qterm))))
                        X_summary.append(np.concatenate((obj_features,
                                         self.get_features_summary(self.entities[bid]['summaries'][attr_name], qterm))))
                        if (bid, qterm) in ground_truth and ground_truth[(bid, qterm)] > 0:
                            y_phrases.append(1)
                            y_summary.append(1)
                        else:
                            y_phrases.append(0)
                            y_summary.append(0)

                        # generate a random predicate per attribute
                        for attr in self.obj_attrs:
                            attr_type = self.obj_attrs[attr]['type']
                            op = '='
                            oprand = ''
                            val = self.entities[bid][attr]
                            obj_res = True
                            if attr_type == 'bool':
                                oprand = random.choice(['true', 'false'])
                                obj_res = (oprand.lower() == val.lower())
                            elif attr_type == 'cate':
                                oprand = random.choice(self.obj_attrs[attr]['values'])
                                obj_res = (oprand.lower() == val.lower())
                            else:
                                oprand = random.randint(int(self.obj_attrs[attr]['range'][0]),
                                                        int(self.obj_attrs[attr]['range'][1]))
                                op = random.choice(['<', '>'])
                                if op == '<':
                                    obj_res = (float(val) < float(oprand))
                                else:
                                    obj_res = (float(val) > float(oprand))

                            obj_features = self.get_features_obj(attr_type, val, attr, op, oprand)
                            X_phrases.append(np.concatenate((obj_features,
                                             self.get_features_phrases(self.entities[bid]['histogram'][attr_name], qterm))))
                            X_summary.append(np.concatenate((obj_features,
                                             self.get_features_summary(self.entities[bid]['summaries'][attr_name], qterm))))
                            if ((bid, qterm) in ground_truth and ground_truth[(bid, qterm)] > 0) or obj_res:
                                y_phrases.append(1)
                                y_summary.append(1)
                            else:
                                y_phrases.append(0)
                                y_summary.append(0)

            X_summary, X_summary_test, y_summary, y_summary_test = \
                train_test_split(np.array(X_summary), y_summary, test_size=0.33)
            marker_model = LogisticRegression().fit(X_summary, y_summary)

            X_phrases, X_phrases_test, y_phrases, y_phrases_test = \
                train_test_split(np.array(X_phrases), y_phrases, test_size=0.33)
            phrase_model = LogisticRegression().fit(X_phrases, y_phrases)

            print('phrase model score = %f' % phrase_model.score(X_phrases_test, y_phrases_test))
            print('marker model score = %f' % marker_model.score(X_summary_test, y_summary_test))
            return phrase_model, marker_model

        self.phrase_model, self.marker_model = train_scorer()

    def clear_cache(self):
        """
        clear the membership function's cache and the interpreter's cache (for experiment purpose).
        """
        self.phrase2vec_cache = {}
        self.membership_cache = {}
        self.interpret_cache = {}

    def phrase2vec(self, phrase):
        """
        Compute the vector representation of a phrase as the normalized average of
        the tokens' word vectors weighted by idf.
        Args:
            phrase (str): the input phrase
        Returns:
            A 300d vector
        """
        if phrase in self.phrase2vec_cache:
            return self.phrase2vec_cache[phrase]

        words = gensim.utils.simple_preprocess(phrase)
        res = np.zeros(300)
        for w in words:
            if w in self.model.wv:
                v = self.model.wv[w] * self.idf[w]
                res += v
        #if phrase in self.phrase_sentiments and self.phrase_sentiments[phrase] < 0:
        #    res = -res
        norm = np.linalg.norm(res)
        if norm > 0:
            res /= norm

        self.phrase2vec_cache[phrase] = res
        return res

    def cosine(self, vec1, vec2):
        """
        Compute the cosine similarity of two vectors
        Args:
            vec1 (np.array): the first vector
            vec2 (np.array): the second vector
        Returns:
            float: the cosine similarity
        """
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        if norm1 > 0 and norm2 > 0:
            return np.dot(vec1, vec2) / norm1 / norm2
        else:
            return 0.0
        # return 1.0 - spatial.distance.cosine(vec1, vec2)

    def get_marker(self, attr, phrase):
        """ Find the closest marker to the input phrase.
        Args:
            attr (string): the subjective attribute name
            phrase (string): the input phrase
        Returns:
            string: a string representing the best matching marker
        """
        vec = self.phrase2vec(phrase)
        best_match = None
        best_sim = 0.0
        for entity in self.entities.values():
            if attr in entity['summaries']:
                for marker in entity['summaries'][attr]:
                    marker_vec = marker['center']
                    sim = self.cosine(vec, marker_vec)
                    if best_match == None or sim >= best_sim:
                        best_match = marker['verbalized']
                        best_sim = sim
        return best_match

    def get_features_obj(self, atype, value, attr, op, oprand):
        """Compute the features for an objective predicates.

        The features are designed as: bool_feature + cate_features + numeric_features

        bool_feature: [1] if disagree otherwise [0]
        cate_features: a binary vector where 1 indicates the category appears in value
                       and -1 indicates that the category appears in oprand
        num_features: [abs_diff, rel_diff], the absolute difference and
                      the relative difference, 0 if the condition is True.

        Args:
            atype: the attribute type
            value: the attribute's value of the instance
            attr: attribute name
            op: the operator
            oprand: the target value
        Returns:
            list
        """
        result = []
        if atype == 'bool' and oprand.lower() != value.lower():
            result.append(1)
        else:
            result.append(0)

        cate_list = [0] * len(self.cate_to_id)
        if atype == 'cate':
            cate_list[self.cate_to_id[value]] += 1
            cate_list[self.cate_to_id[oprand]] -= 1
        result += cate_list

        abs_diff = rel_diff = 0.0
        if atype == 'num':
            value = float(value)
            oprand = float(oprand)
#             if (op == '<' and value < oprand) or \
#                (op == '>' and value > oprand):
#                 abs_diff = rel_diff = 0.0
#             else:
            if op == '<':
                abs_diff = value - oprand
            else:
                abs_diff = oprand - value
            rel_diff = abs_diff / (self.obj_attrs[attr]['range'][1] - \
                                   self.obj_attrs[attr]['range'][0] + 1.0)

        result += [abs_diff, rel_diff]
        return result


    def get_features_phrases(self, histogram, qterm):
        """Compute the features without using the markers. The features include
        the total positive/negative counts, sum of sentiments, number of similar
        phrases etc.
        Args:
            histogram (Dictionary): a counter dictionary of extracted phrases
                of a subjective attribute
            qterm (string): the input query term
        Returns:
            np.array: an array representing the features
        """
        qvec = self.phrase2vec(qterm)
        # count number of similar phrases
        sim_count = 1.0
        count2 = 1.0
        sent_sum = 0.0
        sent_sum2 = 0.0
        pos_count = 0.0
        neg_count = 0.0
        pos_match_count = 0.0
        neg_match_count = 0.0

        sum_phrases = np.zeros(300)
        for phrase in histogram:
            pvec = self.phrase2vec(phrase)
            sum_phrases += pvec * histogram[phrase]
            if self.cosine(qvec, pvec) > 0.8:
                sim_count += histogram[phrase]
                sent_sum += histogram[phrase] * self.phrase_sentiments[phrase]
                if self.phrase_sentiments[phrase] >= 0:
                    pos_match_count += histogram[phrase]
                else:
                    neg_match_count += histogram[phrase]

            sent_sum2 += histogram[phrase] * self.phrase_sentiments[phrase]
            count2 += histogram[phrase]
            if self.phrase_sentiments[phrase] >= 0:
                pos_count += histogram[phrase]
            else:
                neg_count += histogram[phrase]

        X = []
        X.append(sim_count)
        X.append(sent_sum / sim_count)
        X.append(sent_sum2 / count2)
        X.append(pos_count)
        X.append(neg_count)
        X.append(pos_match_count)
        X.append(neg_match_count)
        X.append(self.cosine(qvec, sum_phrases))
        return np.array(X)

    def get_features_summary(self, summary, qterm, num_markers=10):
        """Compute the features from the markers. The features include
        the markers' size, total/average sentiments, and overall similarity
        with the query term.
        Args:
            summary (List): a list of summary objects of a subjective attribute
            qterm (string): the input query term
        Returns:
            np.array: an array representing the features
        """
        qvec = self.phrase2vec(qterm)
        num_marker = len(summary)
        summary.sort(key=lambda x : x['sum_senti'] / (x['size'] + 1))
        X = []
        for marker in summary:
            similarity = self.cosine(marker['center'], qvec)
            X.append(marker['sum_senti'])
            X.append(marker['size'])
            X.append(marker['sum_senti'] / (marker['size'] + 1))
            X.append(similarity)
            X.append(marker['sum_senti'] / (marker['size'] + 1) * similarity)
        for _ in range(num_markers - len(summary)):
            X += [0.0] * 5

        return X

    def interpret(self, query_term, fallback_threshold=0.4):
        """
        The query interpreter method. It tries the NN method first. If the best
        similarity is below the fallback threshold, the co-occurrence method is
        called.
        Args:
            query_term (string): the input query term
            fallback_threshold (float): the similarity threshold for falling back
        Returns:
            (string, string): the interpreted subjective attribute and
                the matching linguistic variant
        """

        if query_term in self.interpret_cache:
            return self.interpret_cache[query_term]
        query_len = len(gensim.utils.simple_preprocess(query_term))
        vector = self.phrase2vec(query_term)
        kd_tree_res = self.kd_tree.query([vector], k=1)
        phrase_id = kd_tree_res[1][0][0]
        res = self.all_phrases[phrase_id]

        # fall back if similarity is too low
        phrase = res[1]
        phrase_vec = self.phrase2vec(phrase)
        similarity = self.cosine(phrase_vec, vector)
        if similarity < fallback_threshold or query_len == 1: # 0.4
            cooc_res = self.cooc.interpret(query_term)
            if cooc_res[0] != None:
                res = cooc_res

        self.interpret_cache[query_term] = res
        return res

    def opine(self, query, bids=None, mode='marker', obj_context='sensitive'):
        """
        Compute the ranking score for all entities.
        Args:
            query (List of strings): a list of query terms
            bids (List): the list of business_id's to be ranked
            mode (string): to indicate whether to compute the scores using
                either the markers or wihtout the markers
            obj_context (string): one of {sensitive, agnostic, boolean};
                how to process the objective predicates
        Returns:
            List of strings: a sorted list of bids' by their scores
        """

        def membership(bid, attr_name, qterm, obj_pred=None):
            if len(self.obj_attrs) == 0:
                if (bid, attr_name, qterm) in self.membership_cache:
                    return self.membership_cache[(bid, attr_name, qterm)]

                if mode == 'marker':
                    if 'summaries' in self.entities[bid] and attr_name in self.entities[bid]['summaries']:
                        summary = self.entities[bid]['summaries'][attr_name]
                        score = self.marker_model.predict_proba([self.get_features_summary(summary, qterm)])[0][1]
                    else:
                        score = 1e-6
                else:
                    if 'histogram' in self.entities[bid] and attr_name in self.entities[bid]['histogram']:
                        histogram = self.entities[bid]['histogram'][attr_name]
                        score = self.phrase_model.predict_proba([self.get_features_phrases(histogram, qterm)])[0][1]
                    else:
                        score = 1e-6
                self.membership_cache[(bid, attr_name, qterm)] = score
                return score
            else:
                if (bid, attr_name, qterm, obj_pred) in self.membership_cache:
                    return self.membership_cache[(bid, attr_name, qterm, obj_pred)]
                if obj_pred == None:
                    atype, attr, op, oprand, value = '', '', '', '', ''
                else:
                    atype, attr, op, oprand = obj_pred
                    if attr not in self.entities[bid]:
                        atype, attr, op, oprand, value = '', '', '', '', ''
                    else:
                        value = self.entities[bid][attr]
                obj_features = self.get_features_obj(atype, value, attr, op, oprand)
                if mode == 'marker':
                    if 'summaries' in self.entities[bid] and attr_name in self.entities[bid]['summaries']:
                        summary = self.entities[bid]['summaries'][attr_name]
                        features = np.concatenate((obj_features, self.get_features_summary(summary, qterm)))
                        score = self.marker_model.predict_proba([features])[0][1]
                    else:
                        score = 1e-6
                else:
                    if 'histogram' in self.entities[bid] and attr_name in self.entities[bid]['histogram']:
                        histogram = self.entities[bid]['histogram'][attr_name]
                        features = np.concatenate((obj_features, self.get_features_phrases(histogram, qterm)))
                        score = self.phrase_model.predict_proba([features])[0][1]
                    else:
                        score = 1e-6
                self.membership_cache[(bid, attr_name, qterm, obj_pred)] = score
                return score

        if bids == None:
            bids = list(self.entities.keys())
        scores = {bid : 1.0 for bid in bids}

        # subjective predicates
        sub_predicates = []
        obj_predicates = []
        for qterm in query:
            if ':' in qterm:
                attr, op, oprand = qterm.split(' ')
                atype, attr = attr.split(':')
                obj_predicates.append((atype, attr, op, oprand))
            else:
                sub_predicates.append(qterm)

        # subjective predicates
        for qterm in sub_predicates:
            qterm = qterm.lower()
            attr_name, _ = self.interpret(qterm)
            for bid in bids:
                scores[bid] *= membership(bid, attr_name, qterm)

        # objective predicates
        if obj_context == 'sensitive':
            # context sensitive
            for obj_pred in obj_predicates:
                # random coupling
                # qterm = random.choice(sub_predicates)
                # match with every subjective predicates
                for qterm in sub_predicates:
                    qterm = qterm.lower()
                    attr_name, _ = self.interpret(qterm)
                    for bid in bids:
                        mem = membership(bid, attr_name, qterm, obj_pred)
                        scores[bid] *= mem
        elif obj_context == 'agnostic':
            import math
            def sigmoid(x):
                return 1 / (1 + math.exp(-x))
            # context agnostic
            for atype, attr, op, oprand in obj_predicates:
                for bid in bids:
                    if attr not in self.entities[bid]:
                        continue
                    val = self.entities[bid][attr]
                    if atype == 'bool':
                        if val.lower() != oprand.lower():
                            scores[bid] *= 0.5 # tune this parameter
                    elif atype == 'cate':
                        if val != oprand:
                            scores[bid] *= 0.3 # tune this parameter
                    else:
                        max_val = self.obj_attrs[attr]['range'][1]
                        min_val = self.obj_attrs[attr]['range'][0]
                        diff = (float(oprand) - float(val)) / (max_val - min_val + 1.0)
                        if op == '<':
                            scores[bid] *= sigmoid(diff)
                        else:
                            scores[bid] *= sigmoid(-diff)
        elif obj_context == 'boolean':
            # boolean
            for atype, attr, op, oprand in obj_predicates:
                for bid in bids:
                    if attr not in self.entities[bid]:
                        continue
                    val = self.entities[bid][attr]
                    if atype == 'bool':
                        if val.lower() != oprand.lower():
                            scores[bid] *= 1e-15
                    elif atype == 'cate':
                        if val != oprand:
                            scores[bid] *= 1e-15
                    else:
                        if op == '<':
                            if float(val) >= float(oprand):
                                scores[bid] *= 1e-15
                        else:
                            if float(val) <= float(oprand):
                                scores[bid] *= 1e-15

        return sorted(bids, key=lambda x : -scores[x])
