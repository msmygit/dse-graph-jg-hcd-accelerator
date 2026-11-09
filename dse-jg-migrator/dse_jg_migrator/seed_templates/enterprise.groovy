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
    // UDT and tuple values (Core syntax verified on DSE 6.9): fields in the UDT's declared order.
    .property('home_address', typeOf('address').create("${it} Main St", 'Springfield', String.format('%05d', it)))
    .property('salary_range', tupleOf(Int, Int).create(50000 + it * 1000, 80000 + it * 1000))
    .iterate()
}

projectCount = Math.min(Math.max(1, (int)({{VERTEX_COUNT}}.intdiv(10))), 100)
projectCount.times {
  g.addV('project')
    .property('project_id', UUID.randomUUID())
    .property('name', "Project_${it}")
    .property('location', Geo.point(-73.9 + it * 0.1, 40.7 + it * 0.1))
    .iterate()
}

// {{EDGE_COUNT}} distinct (employee, project) pairs: employee k -> project (k + k.intdiv(n)) mod p.
employees = g.V().hasLabel('employee').toList()
projects  = g.V().hasLabel('project').toList()
n = employees.size()
{{EDGE_COUNT}}.times { k ->
  g.addE('works_on').from(employees.get(k % n)).to(projects.get((k % n + k.intdiv(n)) % projects.size()))
    .property('since', java.time.LocalDate.now())
    .property('role', ['lead','contributor','reviewer'].get(k % 3))
    .iterate()
}
