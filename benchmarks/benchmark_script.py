import os
import sys
import abc
import time
import random
import psutil
import logging
import argparse
import resource
import multiprocessing

import h5py
import numpy
import nmslib

from n2 import HnswIndex
from metrics import knn_recall, metrics
from download_dataset import get_dataset_fn, DATASETS


try:
    xrange
except NameError:
    xrange = range


CACHE_DIR = './cache'
RESULT_DIR = './result'


logging.basicConfig(stream=sys.stdout, format='%(message)s')
n2_logger = logging.getLogger("n2_benchmark")
n2_logger.setLevel(logging.INFO)


# Set resource limits to prevent memory bombs
memory_limit = 64 * 2**30
soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
if soft == resource.RLIM_INFINITY or soft >= memory_limit:
    n2_logger.debug('resetting memory limit from {0} to {1}. '.format(soft, memory_limit))
    resource.setrlimit(resource.RLIMIT_DATA, (memory_limit, hard))


class BaseANN(object):
    @abc.abstractmethod
    def fit(self, X):
        pass

    @abc.abstractmethod
    def query(self, v, n):
        pass

    @abc.abstractmethod
    def __str__(self):
        pass

    def get_memory_usage(self):
        return psutil.Process().memory_info().rss / 1024


class N2(BaseANN):
    def __init__(self, m, ef_construction, n_threads, ef_search, metric):
        self.name = "N2_M%d_efCon%d_n_thread%s_efSearch%d" % (m, ef_construction, n_threads, ef_search)
        self._m = m
        self._m0 = m * 2
        self._ef_construction = ef_construction
        self._n_threads = n_threads
        self._ef_search = ef_search
        self._index_name = os.path.join(CACHE_DIR, "index_n2_%s_M%d_efCon%d_n_thread%s"
                                        % (args.dataset, m, ef_construction, n_threads))
        self._metric = metric

    def fit(self, X):
        if self._metric == 'euclidean':
            self._n2 = HnswIndex(X.shape[1], 'L2')
        else:
            self._n2 = HnswIndex(X.shape[1])

        if os.path.exists(self._index_name):
            n2_logger.info("Loading index from file")
            self._n2.load(self._index_name, use_mmap=False)
            return

        n2_logger.info("Create Index")
        for i, x in enumerate(X):
            self._n2.add_data(x)
        self._n2.build(m=self._m, max_m0=self._m0, ef_construction=self._ef_construction, n_threads=self._n_threads)
        self._n2.save(self._index_name)

    def query(self, v, n):
        return self._n2.search_by_vector(v, n, self._ef_search)

    def __str__(self):
        return self.name


class NmslibHNSW(BaseANN):
    def __init__(self, m, ef_construction, n_threads, ef_search, metric):
        self.name = "nmslib_M%d_efCon%d_n_thread%s_efSearch%d" % (m, ef_construction, n_threads, ef_search)
        self._index_param = [
            'M=%d' % m,
            'indexThreadQty=%d' % n_threads,
            'efConstruction=%d' % ef_construction,
            'post=0', 'delaunay_type=2']
        self._query_param = ['efSearch=%d' % ef_search]
        self._index_name = os.path.join(CACHE_DIR, "index_nmslib_%s_M%d_efCon%d_n_thread%s"
                                        % (args.dataset, m, ef_construction, n_threads))
        self._metric = {'angular': 'cosinesimil', 'euclidean': 'l2'}[metric]

    def fit(self, X):
        self._index = nmslib.init(self._metric, [], "hnsw", nmslib.DataType.DENSE_VECTOR, nmslib.DistType.FLOAT)

        if os.path.exists(self._index_name):
            logging.info("Loading index from file")
            nmslib.loadIndex(self._index, self._index_name)
        else:
            logging.info("Create Index")
            for i, x in enumerate(X):
                self._index.addDataPoint(i, x)

            nmslib.createIndex(self._index, self._index_param)
            nmslib.saveIndex(self._index, self._index_name)

        nmslib.setQueryTimeParams(self._index, self._query_param)

    def query(self, v, n):
        return nmslib.knnQuery(self._index, n, v)

    def free_index(self):
        nmslib.freeIndex(self._index)

    def __str__(self):
        return self.name


def run_algo(args, library, algo, results_fn):
    n2_logger.info('algo: {0}'.format(algo))
    pool = multiprocessing.Pool()
    pool.close()
    pool.join()

    X_train = load_train_data(args.dataset)

    memory_usage_before = algo.get_memory_usage()
    t0 = time.time()
    algo.fit(X_train)
    build_time = time.time() - t0
    index_size_kb = algo.get_memory_usage() - memory_usage_before
    n2_logger.info('Built index in {0}, Index size: {1}KB'.format(build_time, index_size_kb))

    X_test, nn_dists = load_test_data(args.dataset)

    best_search_time = float('inf')
    best_recall = 0.0  # should be deterministic but paranoid
    try_count = args.try_count
    for i in xrange(try_count):  # Do multiple times to warm up page cache, use fastest
        recall = 0.0
        search_time = 0.0
        for j, v in enumerate(X_test):
            sys.stderr.write("[%d/%d][algo: %s] Querying: %d / %d \r"
                             % (i+1, try_count, str(algo), j+1, len(X_test)))
            t0 = time.time()
            found = algo.query(v, args.count)
            search_time += (time.time() - t0)

            found = [float(metrics[args.distance]['distance'](v, X_train[k])) for k in found]
            recall += knn_recall(nn_dists[j], found, args.count)

            if len(found) < args.count:
                n2_logger.debug('found: {0}, correct: {1}'.format(len(found), args.count))

        sys.stderr.write("\n")

        search_time /= len(X_test)
        recall /= len(X_test)
        best_search_time = min(best_search_time, search_time)
        best_recall = max(best_recall, recall)
        n2_logger.info('[%d/%d][algo: %s] search time: %s, recall: %.5f'
                       % (i+1, try_count, str(algo), str(search_time), recall))

    output = '\t'.join(map(str, [library, algo.name, build_time, best_search_time, best_recall, index_size_kb]))
    with open(results_fn, 'a') as f:
        f.write(output + '\n')

    n2_logger.info('Summary: {0}\n'.format(output))


def load_train_data(which):
    return load_data(which, lambda x: numpy.array(x['train']))


def load_test_data(which):
    def load(x):
        test = numpy.array(x['test'])
        try:
            distances = numpy.array(x['distances'])
        except KeyError:
            if which in ['youtube1m-40-angular', 'youtube-40-angular']:
                n2_logger.error('Your "%s" dataset may be outdated. Remove it and download again.' % which)
            sys.exit('"distances" does not exists in the hdf5 database.')
        return test, distances
    return load_data(which, load)


def load_data(which, method):
    hdf5_fn = get_dataset_fn(which)
    with h5py.File(hdf5_fn, 'r') as f:
        ret = method(f)
    return ret


def get_fn(file_type, args, base=CACHE_DIR):
    fn = '%s_%s_%d_%d' % (os.path.join(base, file_type), args.dataset, args.count, args.random_state)
    return fn


def run(args):
    results_fn = get_fn('result', args, base=RESULT_DIR) + '.txt'

    index_params = [(12, 100)]
    query_params = [25, 50, 100, 250, 500, 750, 1000, 1500, 2500, 5000, 10000]

    algos = {
        'n2': [N2(M, ef_con, args.n_threads, ef_search, args.distance)
               for M, ef_con in index_params
               for ef_search in query_params],
        'nmslib': [NmslibHNSW(M, ef_con, args.n_threads, ef_search, args.distance)
                   for M, ef_con in index_params
                   for ef_search in query_params],
    }

    if args.algo:
        algos = {args.algo: algos[args.algo]}

    algos_flat = [(k, v) for k, vals in algos.items() for v in vals]
    random.shuffle(algos_flat)
    n2_logger.debug('order: %s' % str([a.name for l, a in algos_flat]))

    for library, algo in algos_flat:
        # Spawn a subprocess to force the memory to be reclaimed at the end
        p = multiprocessing.Process(target=run_algo, args=(args, library, algo, results_fn))
        p.start()
        p.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--distance', help='Distance metric', default='angular', choices=['angular', 'euclidean'])
    parser.add_argument('--count', '-k', help="the number of nn to search for", type=int, default=100)
    parser.add_argument('--try_count', help='Number of test attempts', type=int, default=3)
    parser.add_argument('--dataset', help='Which dataset',  default='glove-100-angular', choices=DATASETS)
    parser.add_argument('--n_threads', help='Number of threads', type=int, default=10)
    parser.add_argument('--random_state', help='Random seed', type=int, default=3)
    parser.add_argument('--algo', help='Algorithm', type=str, choices=['n2', 'nmslib'])
    parser.add_argument('--verbose', '-v', help='Print verbose log', action='store_true')
    args = parser.parse_args()

    if not os.path.exists(get_dataset_fn(args.dataset)):
        raise IOError('Please download the dataset')

    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

    if not os.path.exists(RESULT_DIR):
        os.makedirs(RESULT_DIR)

    if args.verbose:
        n2_logger.setLevel(logging.DEBUG)

    random.seed(args.random_state)

    run(args)
