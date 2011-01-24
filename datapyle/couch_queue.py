from __future__ import division
from pytools import Record
import numpy
import uuid
import datetime
from time import time
import cPickle as pickle
import zlib
import base64
from couchdb.http import ResourceNotFound, ResourceConflict



# {{{ sample job --------------------------------------------------------------
class SleepJob(Record):
    def __init__(self, sleep_duration):
        Record.__init__(self,
                sleep_duration=sleep_duration)

    def get_parameter_dict(self):
        return {"sleep_duration": self.sleep_duration}

    def __call__(self):
        from time import time, sleep
        from socket import gethostname
        start = time()
        sleep(self.sleep_duration)
        stop = time()
        return {
                "start": start,
                "stop": stop,
                "host": gethostname()
                }

def generate_sleep_jobs():
    from random import random
    for i in range(100):
        yield SleepJob(random())

# }}}

# {{{ couch helpers

def force_update(db, key, doc):
    while True:
        try:
            db[key] = doc
        except ResourceConflict:
            old_doc = db[key]
            doc['_id'] = old_doc['_id']
            doc['_rev'] = old_doc['_rev']
        else:
            return

def generate_all_docs(couch_db, chunk_size=1000):
    # stupid python-couchdb slurps in the whole result set, work around that

    start_key = None
    skip_first = False
    while True:
        view_opts = {}
        if start_key is not None:
            view_opts["startkey"] = start_key

        all_view = couch_db.view("_all_docs", 
                limit=chunk_size, include_docs="true", **view_opts)

        last_key = None
        count = 0
        for row in all_view:
            if not skip_first:
                yield row.doc
                count += 1
            else:
                skip_first = False

            last_key = row.id

        if count == 0:
            return

        start_key = last_key
        skip_first = True

# }}}

# {{{ queue management --------------------------------------------------------

def update_views(couch_db):
    force_update(couch_db, '_design/job_queue', {
        "language": "javascript",
        "views": {
            "available-jobs": {
                "map": """
                function(doc)
                {
                  if (doc.type && doc.type == "job" && doc.j_state == "av")
                    emit([doc.run, doc._id], {"_id": doc._id});
                }
                """,
                },
            }
        })




def populate_queue(job_generator, couch_db, run_descr, other_metadata={}):
    update_views(couch_db)

    job_count = 0

    other_metadata = other_metadata.copy()

    docs = []

    def upload_docs(docs):
        update_res = couch_db.update(docs)
        for success, docid, rev_or_exc in update_res:
            if not success:
                raise rev_or_exc

        docs[:] = []

        # ping the view to get it updated
        iter(couch_db.view("job_queue/available-jobs", limit=1)).next()


    for num, job in enumerate(job_generator()):
        doc = {
                "_id": str(uuid.uuid4()).replace("-", "").lower(),
                "type": "job",
                "j_state": "av",
                "create_t": time(),
                "dat": base64.encodestring(
                    zlib.compress(pickle.dumps(job, protocol=-1))),
                "run": run_descr,
                }

        doc.update(other_metadata.copy())

        docs.append(doc)

        if len(docs) >= 1000:
            upload_docs(docs)

    upload_docs(docs)



def serve_queue(couch_db, run_descr):
    update_views(couch_db)

    hexchars = "0123456789abcdef"

    from random import seed, choice, randrange
    seed()

    empty_view_count = 0

    from socket import gethostname
    hostname = gethostname()

    from os import getpid
    pid = getpid()

    queue_serve_start_time = time()

    while True:
        view_opts = {
                "limit": 400,
                "include_docs": "true",
                "endkey": [run_descr, "z"], # z sorts after all hex chars
                }

        unrestricted = empty_view_count > 5
        if not unrestricted:
            view_opts["startkey"] = [
                    run_descr, "".join(choice(hexchars) for i in range(5))]
        else:
            view_opts["startkey"] = [ run_descr,]

        available_jobs = couch_db.view("job_queue/available-jobs", **view_opts)

        job_docs = [row.doc for row in available_jobs]

        print "[pid %d] got %d jobs" % (pid, len(job_docs))

        if len(job_docs) == 0:
            if unrestricted:
                # we must be done
                return
            else:
                empty_view_count += 1
                continue

        finished_jobs = []
        while job_docs:
            job_doc = job_docs.pop(randrange(len(job_docs)))

            job = pickle.loads(zlib.decompress(base64.decodestring(str(job_doc["dat"]))))
            # save some space
            del job_doc["dat"]

            job_doc.update(job.get_parameter_dict())

            job_doc["start_t"] = time()
            job_doc.update(job())
            job_doc["fin_t"] = time()

            job_doc["j_proc"] = (hostname, pid, queue_serve_start_time)
            job_doc["j_state"] = "dn"

            finished_jobs.append(job_doc)

        print "[pid %d] finished %d jobs" % len(finished_jobs)

        update_res = couch_db.update(finished_jobs)
        successful_updates = sum(1 for success, doc_id, exc in update_res if success)
        print "[pid %d] submitted %d jobs, %d updated successfully" % (
                pid, len(finished_jobs), successful_updates)
        if successful_updates < len(finished_jobs):
            disp_count = 0
            for success, doc_id, exc in update_res:
                if not success:
                    disp_count += 1
                    if disp_count == 10:
                        break

                    print "[pid %d] fail job %s: %s" % (pid, doc_id, exc)


# }}}

# {{{ couch -> sqlite dumper
def dump_couch_to_sqlite(couch_db, run_descr, outfile, scan_max=None):
    import sqlite3 as sqlite

    # {{{ scan for types
    column_type_dict = {}

    from pytools import ProgressBar
    pb = ProgressBar("scan (pass 1/2)", len(couch_db))
    scan_count = 0
    for doc in generate_all_docs(couch_db):
        if "type" in doc and doc["type"] == "job" and doc["run"] == run_descr:
            for k, v in doc.iteritems():
                if (k in column_type_dict 
                        and column_type_dict[k] != type(v) 
                        and v is not None):
                    raise RuntimeError("ambiguous types for '%s'" % k)
                column_type_dict[k] = type(v)

            scan_count += 1
            if scan_max is not None and scan_count >= scan_max:
                break
        pb.progress()

    pb.finished()
    # }}}

    del column_type_dict["type"]
    column_types = []

    for name, tp in column_type_dict.iteritems():
        if tp == list:
            print "Omitting '%s': can't convert lists" % name
        else:
            column_types.append((name, tp))

    def get_sql_type(tp):
        if tp in (str, unicode):
            return "text"
        elif issubclass(tp, int):
            return "integer"
        elif issubclass(tp, (float, numpy.floating)):
            return "real"
        else:
            raise TypeError("No SQL type for %s" % tp)

    create_stmt = ("create table data (%s)"
            % ",".join("%s %s" % (name, get_sql_type(tp))
                for name, tp in column_types))
    db_conn = sqlite.connect(outfile, timeout=30)
    db_conn.execute(create_stmt)

    insert_stmt = "insert into data values (%s)" % (
            ",".join(["?"]*len(column_types)))

    pb = ProgressBar("fill (pass 2/2)", len(couch_db))
    for doc in generate_all_docs(couch_db):
        data = [None] * len(column_types)
        for i, (col_name, col_tp) in enumerate(column_types):
            if "type" in doc and doc["type"] == "job" and doc["run"] == run_descr:
                try:
                    data[i] = doc[col_name]
                except KeyError:
                    print "doc %s had no field %s" % (doc["_id"], col_name)

        db_conn.execute(insert_stmt, data)
        pb.progress()

    pb.finished()

    db_conn.commit()
    db_conn.close()




# }}}




if __name__ == "__main__":
    import sys
    from couchdb.client import Server as CouchServer
    csrv = CouchServer(sys.argv[1])
    cdb = csrv[sys.argv[2]]

    populate_queue(generate_sleep_jobs, cdb, "sleep")




# vim: foldmethod=marker
