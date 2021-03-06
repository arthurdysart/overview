# -*- coding: utf-8 -*-
from __future__ import print_function
"""
Transforms RDDs from Kafka direct stream (DStream) into capacity, energy, and
power values for given CQL command batch size. Data is reduced by grouping via
primary key: <(0) battery-id, (1) group, (2) cycle>. Separate
database tables are created for capacity and energy. Power calculations setup
but not sent to database.

To use, call from script "spark_submit.sh".
"""

## REQUIRED MODULES
from pyspark import SparkContext
from pyspark.streaming import StreamingContext
from pyspark.streaming.kafka import KafkaUtils as kfk

import cassandra as cass
import cassandra.cluster as cassc
import cassandra.query as cassq
import decouple as dc
import os
import sys


## FUNCTION DEFINITIONS
def stdin(sys_argv):
    """
    Imports Kafka & Cassandra parameters.
    """
    # Sets sensitive variables from ENV file
    try:
        path_home = os.getcwd()
        os.chdir(r"../../util/settings")
        settings = dc.Config(dc.RepositoryEnv(".env"))
        os.chdir(path_home)
    except:
        raise OSError("Cannot import ENV settings. Check path for ENV.")

    # Imports terminal input for simulation & Kafka settings
    try:
        p = {}
        p["spark_name"]= settings.get("SPARK_NAME")
        p["cassandra"] = settings.get("CASSANDRA_MASTER", cast=dc.Csv())
        p["cassandra_key"] = settings.get("CASSANDRA_KEYSPACE")
        p["kafka_brokers"] = settings.get("KAFKA_BROKERS")
        p["kafka_topic"] = settings.get("KAFKA_TOPIC", cast=dc.Csv())
    except:
        raise ValueError("Cannot interpret external settings. Check ENV file.")

    return p

def summarize_step_data(kafka_stream):
    """
    For each entry, calculates capacity, energy, power sum, and counts.
    """
    # Sets constants for mapping calculations
    DELTA_TIME = 1.0
    CAP_CONVERSION = 3.6E6
    ENG_CONVERSION = 3.6E6
    PWR_CONVERSION = 1.0E3

    # For each micro-RDD, strips whitespace and split by comma
    parsed_rdd = kafka_stream.map(lambda ln: \
        tuple(x.strip() for x in ln[1].strip().split(",")))

    # Transforms parsed entries into key-value pair
    # SCHEMA: (<battery id: str>, <group: str>, <cycle: int>, <step: str>) :
    #         (<date-time: str>, <voltage: float>, <current: float>,
    #          <prev_voltage: float>, <step_time: float>)
    paired_rdd = parsed_rdd.map(lambda x: \
        ((str(x[0]), str(x[1]), int(x[2]), str(x[3]),), \
         (str(x[4]), float(x[5]), float(x[6]), float(x[7]), float(x[8]),)))

    # Calculates instantaneous capacity, energy, and power for each entry
    # SCHEMA: (key) :
    #         (<capacity: float>, <energy: float>, <power: float>,
    #          <counts: int>)
    inst_rdd = paired_rdd.map(lambda x: \
        (x[0], \
        (x[1][2] * DELTA_TIME / CAP_CONVERSION, \
         x[1][2] * (x[1][1] + x[1][3]) * DELTA_TIME / (2 * ENG_CONVERSION), \
         x[1][2] * x[1][1] / PWR_CONVERSION, \
         1,)))

    # Calculates total capacity and energy, and power sum for each key
    # SCHEMA: (key) :
    #         (<total capacity: float>, <total energy: float>,
    #          <power sum: float>, <count sum: float>)
    total_rdd = inst_rdd.reduceByKey(lambda i, j: \
        (i[0] + j[0], \
         i[1] + j[1], \
         i[2] + j[2], \
         i[3] + j[3],))

    # Re-organizes key and value contents for Cassandra CQL interpolation
    # SCHEMA: <step: str>, <group: str>, <cycle: int>, <battery id: str>,
    #         <total capacity: float>, <total energy: float>,
    #         <power sum: float>, <counts: float>,
    summary_rdd = total_rdd.map(lambda x: \
        (x[0][3], \
         x[0][1], \
         x[0][2], \
         x[0][0], \
         x[1][0], \
         x[1][1], \
         x[1][2], \
         x[1][3],))

    # Filters according to charge and discharge steps
    discharge_rdd = summary_rdd.filter(lambda x: x[0][0].upper() == "D")
    charge_rdd = summary_rdd.filter(lambda x: x[0][0].upper() == "C")

    return discharge_rdd, charge_rdd

def send_partition(entries, table_name, crit_size=500):
    """
    Collects rdd entries and sends as batch of CQL commands.
    Required by "save_to_database" function.
    """

    # Initializes keyspace and CQL batch executor in Cassandra database
    db_session = cassc.Cluster(p["cassandra"]).connect(p["cassandra_key"])
    cql_batch = cassq.BatchStatement(consistency_level= \
                                     cass.ConsistencyLevel.QUORUM)
    batch_size = 0

    # Prepares CQL statement, with interpolated table name, and placeholders
    cql_command = db_session.prepare("""
                                     UPDATE {} SET
                                     metric =  ? + metric
                                     WHERE group = ?
                                     AND cycle = ?
                                     AND id = ?;
                                     """.format(table_name))

    for e in entries:

        # Interpolates prepared CQL statement with values from entry
        cql_batch.add(cql_command, parameters= \
                      [cassq.ValueSequence((e[3],)), \
                       e[0], \
                       e[1], \
                       e[2],])
        batch_size += 1
        # Executes collected CQL commands, then re-initializes collection
        if batch_size == crit_size:
            db_session.execute(cql_batch)
            cql_batch = cassq.BatchStatement(consistency_level= \
                                             cass.ConsistencyLevel.QUORUM)
            batch_size = 0

    # Executes final set of remaining batches and closes Cassandra session
    db_session.execute(cql_batch)
    db_session.shutdown()

    return None

def save_to_database(input_rdd, table_name):
    """
    For each micro-RDD, sends partition to target database.
    Requires "send_partition" function.
    """
    input_rdd.foreachRDD(lambda rdd: \
        rdd.foreachPartition(lambda entries: \
            send_partition(entries, table_name)))
    return None

def save_to_file(input_rdd, file_name):
    """
    For each micro-RDD, saves input data to text file.

    EXAMPLE:
    save_to_file(parsed_rdd,
                 "/home/ubuntu/overview/src/spark/dstream_stdout/{}.txt")
    """
    input_rdd.foreachRDD(lambda rdd: open(file_name, "a") \
                         .write(str(rdd.collect()) + "\n"))
    return None


## MAIN MODULE
if __name__ == "__main__":
    # Sets Kafka and Cassandra parameters
    p = stdin(sys.argv)
    # Initializes spark context SC and streaming context SCC
    sc = SparkContext(appName=p["spark_name"])
    sc.setLogLevel("WARN")
    ssc = StreamingContext(sc, 10)
    kafka_params = {"metadata.broker.list": p["kafka_brokers"]}
    kafka_stream = kfk.createDirectStream(ssc, \
                                          p["kafka_topic"], \
                                          kafka_params)

    # For each micro-RDD, transforms measurements to summary/overall values
    # SCHEMA: (<group: str>, <cycle: int>,
    #          <battery id: str>, <capacity sum: dbl>, <energy sum: dbl>,
    #          <power sum: dbl>, <counts: int>)
    discharge_rdd, charge_rdd = summarize_step_data(kafka_stream)

    discharge_capacity_rdd = discharge_rdd.map(lambda x: (x[1], \
                                                          x[2], \
                                                          x[3], \
                                                          x[4]))
    save_to_database(discharge_capacity_rdd, "discharge_capacity")


    charge_capacity_rdd = charge_rdd.map(lambda x: (x[1], \
                                                    x[2], \
                                                    x[3], \
                                                    x[4]))
    save_to_database(charge_capacity_rdd, "charge_capacity")


    discharge_energy_rdd = discharge_rdd.map(lambda x: (x[1], \
                                                        x[2], \
                                                        x[3], \
                                                        x[5]))
    save_to_database(discharge_energy_rdd, "discharge_energy")


    charge_energy_rdd = charge_rdd.map(lambda x: (x[1], \
                                                  x[2], \
                                                  x[3], \
                                                  x[5]))
    save_to_database(charge_energy_rdd, "charge_energy")

    # Starts and stops spark streaming context
    ssc.start()
    ssc.awaitTermination()


## END OF FILE