// Template: iot
// Covers: Bigint, Double, Float, Timestamp, composite multi-key index.
// Tokens: {{GRAPH_NAME}}, {{VERTEX_COUNT}}, {{EDGE_COUNT}}

system.graph('{{GRAPH_NAME}}').ifNotExists()
  .withReplication("{'class':'SimpleStrategy','replication_factor':1}")
  .andDurableWrites(true).create()

schema = {{GRAPH_NAME}}.schema()

schema.vertexLabel('device').ifNotExists()
  .partitionBy('device_id', Uuid)
  .property('name', Text)
  .property('type', Text)
  .property('firmware_version', Text)
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

{{VERTEX_COUNT}}.times {
  g.addV('device')
    .property('device_id', UUID.randomUUID())
    .property('name', "Device_${it}")
    .property('type', ['temperature','pressure','humidity','vibration'].get((int)(Math.random()*4)))
    .property('firmware_version', "1.${(int)(Math.random()*10)}.0")
    .iterate()
}

devices = g.V().hasLabel('device').toList()
{{EDGE_COUNT}}.times {
  d = devices.get((int)(Math.random() * devices.size()))
  r = g.addV('reading')
    .property('device_id', g.V(d).values('device_id').next())
    .property('ts', new Date())
    .property('sensor_value', Math.random() * 100)
    .property('raw_value', (long)(Math.random() * Long.MAX_VALUE))
    .property('quality', (float)(Math.random()))
    .next()
  g.addE('produced').from(d).to(r).iterate()
}
