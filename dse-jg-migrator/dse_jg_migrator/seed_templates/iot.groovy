// Template: iot
// Covers: Bigint, Double, Float, Timestamp, Duration (CQL months/days/nanos), Decimal (exact and beyond
// Double precision), composite multi-key index.
// Tokens: {{GRAPH_NAME}}, {{VERTEX_COUNT}}, {{EDGE_COUNT}}; {{V_RANGE}} / {{E_RANGE}} mark a loop the seeder
// submits in chunks (DSE caps each request at 30 s), e.g. (0..<200), (200..<400), … — the index stays global.

system.graph('{{GRAPH_NAME}}').ifNotExists()
  .withReplication("{'class':'SimpleStrategy','replication_factor':1}")
  .andDurableWrites(true).create()

schema = {{GRAPH_NAME}}.schema()

schema.vertexLabel('device').ifNotExists()
  .partitionBy('device_id', Uuid)
  .property('name', Text)
  .property('type', Text)
  .property('firmware_version', Text)
  .property('sampling_interval', Duration)
  .property('calibration_offset', Decimal)
  .create()

schema.vertexLabel('reading').ifNotExists()
  .partitionBy('device_id', Uuid)
  .clusterBy('ts', Timestamp, Desc)
  .property('sensor_value', Double)
  .property('raw_value', Bigint)
  .property('quality', Float)
  .create()

schema.edgeLabel('produced').ifNotExists()
  .from('device').to('reading').create()

schema.vertexLabel('reading').materializedView('reading_by_quality').ifNotExists()
  .partitionBy('device_id').clusterBy('quality', Desc).clusterBy('ts', Desc).create()

schema.vertexLabel('device').secondaryIndex('device_by_type').ifNotExists()
  .by('type').create()

g = {{GRAPH_NAME}}.traversal()

({{V_RANGE}}).each {
  g.addV('device')
    .property('device_id', UUID.randomUUID())
    .property('name', "Device_${it}")
    .property('type', ['temperature','pressure','humidity','vibration'].get((int)(Math.random()*4)))
    .property('firmware_version', "1.${(int)(Math.random()*10)}.0")
    // Core (DSG) Duration is a CQL duration; java.time.Duration is rejected (verified on DSE 6.9).
    .property('sampling_interval', com.datastax.driver.core.Duration.newInstance(it % 3, it % 7, (it % 24) * 3600000000000L))
    // Half exact (19.99), half beyond Double precision — exercises the migrator's LOSSY_DECIMAL count.
    .property('calibration_offset', it % 2 == 0 ? new BigDecimal('19.99') : new BigDecimal("${it}.123456789012345678"))
    .iterate()
}

devices = g.V().hasLabel('device').toList()
({{E_RANGE}}).each {
  d = devices.get((int)(Math.random() * devices.size()))
  r = g.addV('reading')
    .property('device_id', g.V(d).values('device_id').next())
    .property('ts', java.time.Instant.now())
    .property('sensor_value', Math.random() * 100)
    .property('raw_value', (long)(Math.random() * Long.MAX_VALUE))
    .property('quality', (float)(Math.random()))
    .next()
  g.addE('produced').from(d).to(r).iterate()
}
