struct VertexOutput {
    float4 position : SV_Position;
};

VertexOutput main(uint vertex_id : SV_VertexID) {
    VertexOutput output;
    output.position = float4(float(vertex_id), 0.0, 0.0, 1.0);
    return output;
}
