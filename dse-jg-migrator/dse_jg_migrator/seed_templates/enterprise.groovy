// Template: enterprise
// Purpose: DELIBERATELY triggers RED findings in assess output so operators
//          can see what a HIGH/CRITICAL risk assessment looks like.
// Covers: UDT (frozen typeOf), tupleOf, OLAP source, RLAC marker.
// Tokens: {{GRAPH_NAME}}, {{VERTEX_COUNT}}, {{EDGE_COUNT}}

system.graph('{{GRAPH_NAME}}').ifNotExists()
  .withReplication("{'class':'SimpleStrategy','replication_factor':1}")
  .andDurableWrites(true).create()

schema = {{GRAPH_NAME}}.schema()

// UDT definition (triggers SC-005 RED)
schema.type('address').ifNotExists()
  .property('street', Text)
  .property('city', Text)
  .property('zip', Text)
  .create()

schema.vertexLabel('employee').ifNotExists()
  .partitionBy('employee_id', Uuid)
  .property('name', Text)
  .property('dept', Text)
  .property('salary_range', tupleOf(Int, Int))
  .property('home_address', frozen(typeOf('address')))
  .create()

schema.vertexLabel('project').ifNotExists()
  .partitionBy('project_id', Uuid)
  .property('name', Text)
  .property('location', Point)
  .create()

schema.edgeLabel('works_on').ifNotExists()
  .from('employee').to('project')
  .property('since', Date)
  .property('role', Text)
  .create()

schema.vertexLabel('project').searchIndex().ifNotExists()
  .by('name').asText()
  .by('location').create()

// NOTE: RLAC and OLAP enablement are cluster-level and cannot be set via schema DSL.
// The assess command detects OLAP by probing the 'a' traversal source.
// The enterprise template's presence of these patterns is documented in the
// assessment report to help operators understand what RED findings look like.

g = {{GRAPH_NAME}}.traversal()

{{VERTEX_COUNT}}.times {
  g.addV('employee')
    .property('employee_id', UUID.randomUUID())
    .property('name', "Employee_${it}")
    .property('dept', ['Engineering','Finance','Legal','Operations'].get((int)(Math.random()*4)))
    .iterate()
}

projectCount = Math.min({{VERTEX_COUNT}} / 10, 100) as int
projectCount.times {
  g.addV('project')
    .property('project_id', UUID.randomUUID())
    .property('name', "Project_${it}")
    .iterate()
}

employees = g.V().hasLabel('employee').limit({{EDGE_COUNT}}).toList()
projects  = g.V().hasLabel('project').toList()
employees.each { e ->
  p = projects.get((int)(Math.random() * projects.size()))
  g.addE('works_on').from(e).to(p)
    .property('since', new Date())
    .property('role', ['lead','contributor','reviewer'].get((int)(Math.random()*3)))
    .iterate()
}
